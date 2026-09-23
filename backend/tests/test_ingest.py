"""Direct service-level tests for app.services.ingest: heartbeat handling,
composite-identity dedup, firing/resolved transitions, and the
IntegrityError race-fallback path.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.cluster import Cluster
from app.models.team import Team
from app.services import ingest
from app.services.ingest import (
    AlertmanagerAlert,
    AlertmanagerWebhookPayload,
    _parse_am_timestamp,
    ingest_webhook,
)


def _alert(
    *,
    fingerprint: str = "fp-1",
    status: str = "firing",
    alertname: str = "PlatformCritical",
    kam_team: str | None = "platform",
    severity: str | None = "Critical",
    namespace: str | None = "kam-demo",
    starts_at: str | None = "2026-09-22T00:00:00Z",
    ends_at: str | None = None,
) -> AlertmanagerAlert:
    labels = {"alertname": alertname}
    if kam_team is not None:
        labels["kam_team"] = kam_team
    if severity is not None:
        labels["severity"] = severity
    if namespace is not None:
        labels["namespace"] = namespace
    return AlertmanagerAlert(
        status=status,
        labels=labels,
        annotations={"summary": "test"},
        startsAt=starts_at,
        endsAt=ends_at,
        fingerprint=fingerprint,
        generatorURL="http://prom/graph",
    )


async def _get_default_cluster(session: AsyncSession) -> Cluster:
    result = await session.execute(select(Cluster))
    return result.scalars().first()


async def _create_cluster(session: AsyncSession, name: str, **overrides) -> Cluster:
    cluster = Cluster(
        name=name,
        display_name=name,
        prometheus_url="http://prom",
        alertmanager_url="http://am",
        webhook_token_hash=f"hash-{name}",
        **overrides,
    )
    session.add(cluster)
    await session.flush()
    return cluster


async def _create_team(session: AsyncSession, slug: str) -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def test_new_firing_insert_with_team_attribution(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        await _create_team(session, "platform")

        payload = AlertmanagerWebhookPayload(alerts=[_alert()])
        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.received == 1
        assert result.created == 1
        assert result.created_resolved == 0

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.status == "firing"
        assert row.alertname == "PlatformCritical"
        assert row.severity == "critical"  # lowercased
        assert row.namespace == "kam-demo"
        assert row.team_id is not None
        assert row.receive_count == 1


async def test_unknown_team_slug_results_in_null_team(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)

        payload = AlertmanagerWebhookPayload(alerts=[_alert(kam_team="no-such-team")])
        await ingest_webhook(session, cluster, payload)
        await session.commit()

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.team_id is None


async def test_repeat_same_identity_increments_receive_count_not_created(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)

        payload = AlertmanagerWebhookPayload(alerts=[_alert()])
        r1 = await ingest_webhook(session, cluster, payload)
        await session.commit()
        r2 = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert r1.created == 1
        assert r2.created == 0
        assert r2.repeats == 1

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].receive_count == 2


async def test_firing_to_resolved_transition_calls_hook(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)

        firing_payload = AlertmanagerWebhookPayload(alerts=[_alert(status="firing")])
        await ingest_webhook(session, cluster, firing_payload)
        await session.commit()

        resolved_payload = AlertmanagerWebhookPayload(
            alerts=[_alert(status="resolved", ends_at="2026-09-22T00:10:00Z")]
        )
        with patch.object(ingest, "on_event_transition", new=AsyncMock()) as spy:
            result = await ingest_webhook(session, cluster, resolved_payload)
            await session.commit()

        assert result.resolved == 1
        spy.assert_awaited_once()
        _, _, kind = spy.await_args.args
        assert kind == "resolved"

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.status == "resolved"
        assert row.ends_at is not None


async def test_new_firing_calls_hook_with_firing_kind(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(alerts=[_alert(status="firing")])

        with patch.object(ingest, "on_event_transition", new=AsyncMock()) as spy:
            await ingest_webhook(session, cluster, payload)
            await session.commit()

        spy.assert_awaited_once()
        _, _, kind = spy.await_args.args
        assert kind == "firing"


async def test_resolved_first_insert_does_not_call_hook(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(
            alerts=[_alert(status="resolved", ends_at="2026-09-22T00:10:00Z")]
        )

        with patch.object(ingest, "on_event_transition", new=AsyncMock()) as spy:
            result = await ingest_webhook(session, cluster, payload)
            await session.commit()

        assert result.created_resolved == 1
        assert result.created == 0
        spy.assert_not_awaited()

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.status == "resolved"
        assert row.ends_at is not None


async def test_composite_identity_distinguishes_clusters(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster_a = await _get_default_cluster(session)
        cluster_b = await _create_cluster(session, "second")

        payload = AlertmanagerWebhookPayload(alerts=[_alert()])
        await ingest_webhook(session, cluster_a, payload)
        await ingest_webhook(session, cluster_b, payload)
        await session.commit()

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 2
        assert {r.cluster_id for r in rows} == {cluster_a.id, cluster_b.id}


async def test_heartbeat_watchdog_creates_no_event_and_updates_cluster(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        assert cluster.heartbeat_enabled is True
        assert cluster.heartbeat_alertname == "Watchdog"

        payload = AlertmanagerWebhookPayload(
            alerts=[_alert(alertname="Watchdog", kam_team=None, severity=None, namespace=None)]
        )
        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.heartbeats_seen == 1
        assert result.created == 0

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert rows == []

        await session.refresh(cluster)
        assert cluster.heartbeat_state == "ok"
        assert cluster.last_heartbeat_at is not None


async def test_heartbeat_disabled_processes_as_normal_event(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        cluster.heartbeat_enabled = False
        await session.flush()

        payload = AlertmanagerWebhookPayload(
            alerts=[_alert(alertname="Watchdog", kam_team=None, severity=None, namespace=None)]
        )
        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.heartbeats_seen == 0
        assert result.created == 1

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.alertname == "Watchdog"


def test_nano_timestamp_parses() -> None:
    parsed = _parse_am_timestamp("2026-09-22T00:00:00.123456789Z")
    assert parsed == datetime(2026, 9, 22, 0, 0, 0, 123456, tzinfo=UTC)


def test_zero_value_ends_at_parses_to_none() -> None:
    assert _parse_am_timestamp("0001-01-01T00:00:00Z") is None


def test_none_ends_at_parses_to_none() -> None:
    assert _parse_am_timestamp(None) is None


def test_offset_timestamp_normalizes_to_utc() -> None:
    """A non-Z offset must convert to the equivalent UTC instant, not just
    keep its own offset -- otherwise the same instant delivered with two
    different offsets would compare unequal and defeat identity dedup.
    """
    parsed = _parse_am_timestamp("2026-09-22T09:00:00+09:00")
    assert parsed == datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)
    assert parsed.tzinfo == UTC


def test_malformed_timestamp_raises_value_error() -> None:
    with pytest.raises(ValueError):
        _parse_am_timestamp("not-a-timestamp")


# -- C1: UTC normalization / identity dedup across offsets ----------------


async def test_same_instant_different_offset_strings_dedup_to_one_row(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)

        payload_z = AlertmanagerWebhookPayload(
            alerts=[_alert(starts_at="2026-09-22T00:00:00Z")]
        )
        # Same instant as above, expressed with a +09:00 offset instead.
        payload_offset = AlertmanagerWebhookPayload(
            alerts=[_alert(starts_at="2026-09-22T09:00:00+09:00")]
        )

        r1 = await ingest_webhook(session, cluster, payload_z)
        await session.commit()
        r2 = await ingest_webhook(session, cluster, payload_offset)
        await session.commit()

        assert r1.created == 1
        assert r2.created == 0
        assert r2.repeats == 1

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].receive_count == 2


async def test_starts_at_stored_as_utc_aware(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(
            alerts=[_alert(starts_at="2026-09-22T09:00:00+09:00")]
        )
        await ingest_webhook(session, cluster, payload)
        await session.commit()

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.starts_at.tzinfo is not None
        assert row.starts_at == datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)


async def test_resolved_then_firing_reopens_and_calls_hook_with_firing(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)

        await ingest_webhook(
            session, cluster, AlertmanagerWebhookPayload(alerts=[_alert(status="firing")])
        )
        await session.commit()

        await ingest_webhook(
            session,
            cluster,
            AlertmanagerWebhookPayload(
                alerts=[_alert(status="resolved", ends_at="2026-09-22T00:10:00Z")]
            ),
        )
        await session.commit()

        with patch.object(ingest, "on_event_transition", new=AsyncMock()) as spy:
            result = await ingest_webhook(
                session, cluster, AlertmanagerWebhookPayload(alerts=[_alert(status="firing")])
            )
            await session.commit()

        assert result.reopened == 1
        assert result.created == 0
        assert result.repeats == 0
        spy.assert_awaited_once()
        _, _, kind = spy.await_args.args
        assert kind == "firing"

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.status == "firing"
        assert row.ends_at is None
        assert row.receive_count == 3


async def test_batch_with_heartbeat_and_real_alert_handles_both(app) -> None:
    """Pins that a batch mixing a Watchdog heartbeat with a real alert
    processes both correctly instead of one interfering with the other.
    """
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)

        payload = AlertmanagerWebhookPayload(
            alerts=[
                _alert(
                    fingerprint="fp-heartbeat",
                    alertname="Watchdog",
                    kam_team=None,
                    severity=None,
                    namespace=None,
                ),
                _alert(fingerprint="fp-real", alertname="RealAlert"),
            ]
        )
        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.received == 2
        assert result.heartbeats_seen == 1
        assert result.created == 1

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].alertname == "RealAlert"

        await session.refresh(cluster)
        assert cluster.heartbeat_state == "ok"


# -- I3: per-alert isolation for bad timestamps ----------------------------


async def test_malformed_starts_at_is_skipped_not_raised(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(alerts=[_alert(starts_at="not-a-timestamp")])

        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.skipped == 1
        assert result.created == 0
        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert rows == []


async def test_missing_starts_at_is_skipped(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(alerts=[_alert(starts_at=None)])

        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.skipped == 1
        assert result.created == 0


async def test_zero_value_starts_at_is_skipped(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(
            alerts=[_alert(starts_at="0001-01-01T00:00:00Z")]
        )

        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.skipped == 1
        assert result.created == 0


async def test_bad_alert_in_batch_does_not_block_good_alerts(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(
            alerts=[
                _alert(fingerprint="fp-good-1"),
                _alert(fingerprint="fp-bad", starts_at="not-a-timestamp"),
                _alert(fingerprint="fp-good-2"),
            ]
        )

        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.received == 3
        assert result.skipped == 1
        assert result.created == 2

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert {r.fingerprint for r in rows} == {"fp-good-1", "fp-good-2"}


async def test_malformed_ends_at_on_resolved_sets_none_not_skipped(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        payload = AlertmanagerWebhookPayload(
            alerts=[_alert(status="resolved", ends_at="not-a-timestamp")]
        )

        result = await ingest_webhook(session, cluster, payload)
        await session.commit()

        assert result.skipped == 0
        assert result.created_resolved == 1

        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.status == "resolved"
        assert row.ends_at is None


async def test_integrity_error_fallback_updates_existing_row(app) -> None:
    """Simulates a concurrent webhook delivery that already won the race for
    this identity: a conflicting row is pre-inserted directly, and
    `_get_existing`'s first call is forced to (falsely) report "not found",
    so `_ingest_one` attempts the insert anyway. That must hit the UQ,
    roll back to the savepoint, and fall back to re-querying and treating
    it as a repeat of the row that's already there.
    """
    async with db_module.async_session_factory() as session:
        cluster = await _get_default_cluster(session)
        starts_at = datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC)

        session.add(
            AlertEvent(
                cluster_id=cluster.id,
                cluster_name=cluster.name,
                fingerprint="fp-race",
                status="firing",
                alertname="RaceCondition",
                severity=None,
                namespace=None,
                labels={"alertname": "RaceCondition"},
                annotations={},
                team_id=None,
                starts_at=starts_at,
                ends_at=None,
                generator_url=None,
            )
        )
        await session.commit()

        real_get_existing = ingest._get_existing
        call_count = 0

        async def blind_once_get_existing(session_, cluster_id, fingerprint, starts_at_):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None
            return await real_get_existing(session_, cluster_id, fingerprint, starts_at_)

        payload = AlertmanagerWebhookPayload(
            alerts=[
                _alert(
                    fingerprint="fp-race",
                    alertname="RaceCondition",
                    kam_team=None,
                    severity=None,
                    namespace=None,
                )
            ]
        )

        with patch.object(ingest, "_get_existing", new=blind_once_get_existing):
            result = await ingest_webhook(session, cluster, payload)
            await session.commit()

        assert call_count == 2
        assert result.created == 0
        assert result.repeats == 1

        rows = (await session.execute(select(AlertEvent))).scalars().all()
        assert len(rows) == 1
        assert rows[0].receive_count == 2
