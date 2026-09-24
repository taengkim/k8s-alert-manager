"""Tests for app/worker/heartbeat.py: the ok->missing edge-triggered sweep,
its natural pass-through of the synthetic alert through real ingest/routing,
and (via app.services.ingest) the missing->ok recovery side.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select

import app.db as db_module
import app.worker.heartbeat as heartbeat_module
from app.channels.email import EmailConfig
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.team import Team
from app.security import encrypt_str
from app.services.ingest import (
    HEARTBEAT_LOST_ALERTNAME,
    AlertmanagerAlert,
    AlertmanagerWebhookPayload,
    heartbeat_lost_fingerprint,
    ingest_webhook,
)
from app.worker.heartbeat import sweep


async def _create_team(session, slug: str = "platform") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(
    session,
    name: str = "hb-cluster",
    *,
    enabled: bool = True,
    heartbeat_enabled: bool = True,
    heartbeat_state: str = "ok",
    heartbeat_timeout_seconds: int = 60,
    last_heartbeat_at: datetime | None = None,
    heartbeat_team_id: int | None = None,
    heartbeat_alertname: str = "Watchdog",
) -> Cluster:
    cluster = Cluster(
        name=name,
        display_name=name.title(),
        prometheus_url="http://prom",
        alertmanager_url="http://am",
        webhook_token_hash=f"hash-{name}",
        enabled=enabled,
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_state=heartbeat_state,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        last_heartbeat_at=last_heartbeat_at,
        heartbeat_team_id=heartbeat_team_id,
        heartbeat_alertname=heartbeat_alertname,
    )
    session.add(cluster)
    await session.flush()
    return cluster


async def _create_channel(session, team: Team, name: str = "c1") -> Channel:
    channel = Channel(
        team_id=team.id,
        name=name,
        type="email",
        config_encrypted=encrypt_str(EmailConfig(recipients=["ops@example.org"]).model_dump_json()),
    )
    session.add(channel)
    await session.flush()
    return channel


async def _create_notify_rule(session, team: Team, channel: Channel, **fields) -> RoutingRule:
    rule = RoutingRule(
        team_id=team.id, name="r1", action="notify", channels=[channel], **fields
    )
    session.add(rule)
    await session.flush()
    return rule


def _stale(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


# -- state machine table -----------------------------------------------------


async def test_unknown_state_never_alarms(app) -> None:
    """A cluster with no heartbeat history is entirely excluded from the
    sweep's own query -- it can never go 'missing', regardless of how old
    its (nonexistent) last_heartbeat_at is.
    """
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(session, heartbeat_state="unknown", last_heartbeat_at=None)
        await session.commit()
        cluster_id = cluster.id

    summary = await sweep(db_module.async_session_factory)

    assert summary["clusters_checked"] == 0
    assert summary["went_missing"] == []

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "unknown"


async def test_ok_past_timeout_transitions_to_missing_with_one_synthetic_event(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session, heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        await session.commit()
        cluster_id = cluster.id

    summary = await sweep(db_module.async_session_factory)

    assert summary["clusters_checked"] == 1
    assert summary["went_missing"] == ["hb-cluster"]

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "missing"

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].alertname == HEARTBEAT_LOST_ALERTNAME
        assert rows[0].fingerprint == heartbeat_lost_fingerprint(cluster_id)
        assert rows[0].status == "firing"
        assert rows[0].severity == "critical"
        assert rows[0].cluster_id == cluster_id


async def test_ok_within_timeout_stays_ok_no_event(app) -> None:
    async with db_module.async_session_factory() as session:
        await _create_cluster(session, heartbeat_timeout_seconds=600, last_heartbeat_at=_stale(30))
        await session.commit()

    summary = await sweep(db_module.async_session_factory)

    assert summary["clusters_checked"] == 1
    assert summary["went_missing"] == []
    assert (await _all_events()) == []


async def _all_events() -> list[AlertEvent]:
    async with db_module.async_session_factory() as session:
        return (await session.execute(select(AlertEvent))).scalars().all()


async def test_missing_state_is_never_rechecked_no_repeat_alarm(app) -> None:
    """Once 'missing', a cluster is excluded from the sweep's own query
    (state == 'ok' is part of the WHERE clause) -- repeated ticks produce no
    additional synthetic alert while it stays missing.
    """
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session, heartbeat_state="missing", last_heartbeat_at=_stale(9999)
        )
        await session.commit()
        cluster_id = cluster.id

    summary1 = await sweep(db_module.async_session_factory)
    summary2 = await sweep(db_module.async_session_factory)

    assert summary1["clusters_checked"] == 0
    assert summary1["went_missing"] == []
    assert summary2["clusters_checked"] == 0
    assert summary2["went_missing"] == []
    assert (await _all_events()) == []

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "missing"


async def test_recovery_resolves_event_and_flips_state_back_to_ok(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session, heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        await session.commit()
        cluster_id = cluster.id

    await sweep(db_module.async_session_factory)

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "missing"

        payload = AlertmanagerWebhookPayload(
            alerts=[AlertmanagerAlert(status="firing", labels={"alertname": "Watchdog"}, fingerprint="wd-1")]
        )
        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.heartbeats_seen == 1
        await session.refresh(cluster)
        assert cluster.heartbeat_state == "ok"
        assert cluster.last_heartbeat_at is not None

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "resolved"
        assert rows[0].ends_at is not None


async def test_heartbeat_disabled_cluster_is_skipped(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session,
            heartbeat_enabled=False,
            heartbeat_timeout_seconds=60,
            last_heartbeat_at=_stale(120),
        )
        await session.commit()
        cluster_id = cluster.id

    summary = await sweep(db_module.async_session_factory)

    assert summary["clusters_checked"] == 0
    assert summary["went_missing"] == []

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "ok"
    assert (await _all_events()) == []


async def test_disabled_cluster_is_skipped(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session, enabled=False, heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        await session.commit()
        cluster_id = cluster.id

    summary = await sweep(db_module.async_session_factory)

    assert summary["clusters_checked"] == 0
    assert summary["went_missing"] == []

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "ok"
    assert (await _all_events()) == []


# -- routing pass-through -----------------------------------------------------


async def test_synthetic_event_routes_when_heartbeat_team_configured(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = await _create_channel(session, team)
        await _create_notify_rule(session, team, channel)
        cluster = await _create_cluster(
            session,
            heartbeat_timeout_seconds=60,
            last_heartbeat_at=_stale(120),
            heartbeat_team_id=team.id,
        )
        await session.commit()
        cluster_id = cluster.id

    await sweep(db_module.async_session_factory)

    async with db_module.async_session_factory() as session:
        event = (await session.execute(select(AlertEvent))).scalar_one()
        assert event.cluster_id == cluster_id
        assert event.team_id == team.id

        outbox_rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(outbox_rows) == 1
        assert outbox_rows[0].channel_id == channel.id


async def test_synthetic_event_skips_routing_when_no_heartbeat_team(app) -> None:
    """No heartbeat_team_id configured -- Phase 9 semantics: the event still
    gets recorded (visible via history/the Alerts banner) but is never
    routed to any channel.
    """
    async with db_module.async_session_factory() as session:
        await _create_cluster(session, heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120))
        await session.commit()

    await sweep(db_module.async_session_factory)

    async with db_module.async_session_factory() as session:
        event = (await session.execute(select(AlertEvent))).scalar_one()
        assert event.team_id is None
        assert (await session.execute(select(NotificationOutbox))).scalars().all() == []


# -- deterministic fingerprint across recurrences ----------------------------


async def test_repeat_occurrence_after_recovery_creates_new_event_leaves_old_resolved(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session, heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        await session.commit()
        cluster_id = cluster.id

    # First occurrence.
    await sweep(db_module.async_session_factory)

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        payload = AlertmanagerWebhookPayload(
            alerts=[AlertmanagerAlert(status="firing", labels={"alertname": "Watchdog"}, fingerprint="wd-1")]
        )
        await ingest_webhook(session, cluster, payload)
        await session.commit()

    # Simulate the clock moving on and the heartbeat lapsing again.
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        cluster.last_heartbeat_at = _stale(120)
        await session.commit()

    await sweep(db_module.async_session_factory)

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "missing"

        rows = (
            (await session.execute(select(AlertEvent).order_by(AlertEvent.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        assert rows[0].fingerprint == rows[1].fingerprint == heartbeat_lost_fingerprint(cluster_id)
        assert rows[0].starts_at != rows[1].starts_at
        assert rows[0].status == "resolved"
        assert rows[1].status == "firing"


# -- multi-cluster independence ----------------------------------------------


async def test_sweep_handles_multiple_clusters_independently(app) -> None:
    async with db_module.async_session_factory() as session:
        timed_out = await _create_cluster(
            session, name="hb-timed-out", heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        healthy = await _create_cluster(
            session, name="hb-healthy", heartbeat_timeout_seconds=600, last_heartbeat_at=_stale(30)
        )
        await session.commit()
        timed_out_id, healthy_id = timed_out.id, healthy.id

    summary = await sweep(db_module.async_session_factory)

    assert summary["clusters_checked"] == 2
    assert summary["went_missing"] == ["hb-timed-out"]

    async with db_module.async_session_factory() as session:
        assert (await session.get(Cluster, timed_out_id)).heartbeat_state == "missing"
        assert (await session.get(Cluster, healthy_id)).heartbeat_state == "ok"

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].cluster_id == timed_out_id


# -- per-cluster isolation ----------------------------------------------------


async def test_sweep_isolates_per_cluster_failures(app) -> None:
    """A failure injecting one cluster's synthetic alert (e.g. route_event
    raising on a misconfigured team, a DB hiccup, ...) must not roll back or
    block any other candidate in the same sweep tick -- each candidate is
    evaluated/flipped in its own session/transaction.
    """
    async with db_module.async_session_factory() as session:
        failing = await _create_cluster(
            session, name="hb-fail", heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        healthy = await _create_cluster(
            session, name="hb-ok", heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        await session.commit()
        failing_id, healthy_id = failing.id, healthy.id

    real_inject = heartbeat_module._inject_heartbeat_lost

    async def flaky_inject(session, cluster, now):
        if cluster.name == "hb-fail":
            raise RuntimeError("simulated injection failure")
        return await real_inject(session, cluster, now)

    with patch.object(heartbeat_module, "_inject_heartbeat_lost", side_effect=flaky_inject):
        summary = await sweep(db_module.async_session_factory)

    # The failing cluster's own transaction never committed -- its state is
    # untouched (still 'ok'), so it's simply re-evaluated (and, since it's
    # still timed out, retried) on the next tick, per the module docstring.
    assert summary["went_missing"] == ["hb-ok"]

    async with db_module.async_session_factory() as session:
        assert (await session.get(Cluster, failing_id)).heartbeat_state == "ok"
        assert (await session.get(Cluster, healthy_id)).heartbeat_state == "missing"

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].cluster_id == healthy_id

    # And a subsequent tick, once the failure is gone, successfully catches
    # up the cluster that failed the first time.
    summary2 = await sweep(db_module.async_session_factory)
    assert summary2["went_missing"] == ["hb-fail"]
    async with db_module.async_session_factory() as session:
        assert (await session.get(Cluster, failing_id)).heartbeat_state == "missing"
