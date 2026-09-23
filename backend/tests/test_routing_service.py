"""Tests for app.services.routing.route_event: the DB-touching half of the
routing engine (compile_rule/evaluate are covered purely in
test_routing_engine.py). Exercises suppress-first precedence, multi-rule
channel-union dedup via the outbox UQ, the unassigned-team skip, and the
frozen notification payload's content.

The bottom section (Phase 14) exercises route_event's view_notify fan-out:
an owner team's event reaching a target team's own include_shared=true
rules via an AlertShare.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.db as db_module
from app.channels.email import EmailConfig
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingMatcher, RoutingRule
from app.models.share import AlertShare
from app.models.team import Team
from app.security import encrypt_str
from app.services.routing import route_event


async def _create_team(session: AsyncSession, slug: str) -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(session: AsyncSession, name: str = "rt-cluster") -> Cluster:
    cluster = Cluster(
        name=name,
        display_name=name,
        prometheus_url="http://prom",
        alertmanager_url="http://am",
        webhook_token_hash=f"hash-{name}",
    )
    session.add(cluster)
    await session.flush()
    return cluster


async def _create_channel(session: AsyncSession, team: Team, name: str = "email-1") -> Channel:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = Channel(
        team_id=team.id,
        name=name,
        type="email",
        config_encrypted=encrypt_str(config.model_dump_json()),
    )
    session.add(channel)
    await session.flush()
    return channel


async def _create_rule(
    session: AsyncSession,
    team: Team,
    *,
    name: str,
    action: str = "notify",
    channels: list[Channel] | None = None,
    matchers: list[RoutingMatcher] | None = None,
    **fields,
) -> RoutingRule:
    rule = RoutingRule(
        team_id=team.id,
        name=name,
        action=action,
        channels=channels or [],
        matchers=matchers or [],
        **fields,
    )
    session.add(rule)
    await session.flush()
    return rule


async def _create_event(
    session: AsyncSession,
    cluster: Cluster,
    team: Team | None,
    *,
    alertname: str = "HighCpu",
    severity: str | None = "critical",
    namespace: str | None = "kam-demo",
    status: str = "firing",
    annotations: dict[str, str] | None = None,
) -> AlertEvent:
    event = AlertEvent(
        cluster_id=cluster.id,
        cluster_name=cluster.name,
        fingerprint="fp-1",
        status=status,
        alertname=alertname,
        severity=severity,
        namespace=namespace,
        labels={"alertname": alertname},
        annotations=annotations or {},
        team_id=team.id if team else None,
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(event)
    await session.flush()
    return event


async def test_unassigned_team_is_skipped(app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(session)
        event = await _create_event(session, cluster, team=None)

        outcome = await route_event(session, event, "firing")

        assert outcome.routed is False
        assert outcome.reason == "unassigned_team"
        assert (
            await session.execute(select(NotificationOutbox))
        ).scalars().all() == []


async def test_suppress_rule_matching_blocks_all_notification(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        await _create_rule(
            session, team, name="suppress-critical", action="suppress", severities=["critical"]
        )
        await _create_rule(
            session, team, name="notify-all", action="notify", channels=[channel]
        )
        event = await _create_event(session, cluster, team, severity="critical")

        outcome = await route_event(session, event, "firing")

        assert outcome.routed is False
        assert outcome.reason == "suppressed"
        assert event.suppressed_by_rule_id is not None
        assert (
            await session.execute(select(NotificationOutbox))
        ).scalars().all() == []


async def test_multi_rule_channel_union_dedup(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel_a = await _create_channel(session, team, name="a")
        channel_b = await _create_channel(session, team, name="b")
        # Both rules match this event and both include channel_a -- it must
        # get exactly one outbox row (union, deduped), while channel_b
        # (only in rule 2) gets its own row.
        await _create_rule(session, team, name="r1", channels=[channel_a])
        await _create_rule(session, team, name="r2", channels=[channel_a, channel_b])
        event = await _create_event(session, cluster, team)

        outcome = await route_event(session, event, "firing")
        await session.commit()

        assert outcome.routed is True
        assert outcome.channels_notified == 2

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert {r.channel_id for r in rows} == {channel_a.id, channel_b.id}
        assert all(r.status == "pending" for r in rows)
        assert all(r.alert_event_id == event.id for r in rows)


async def test_route_event_is_idempotent_via_outbox_uq(app) -> None:
    """A repeated call for the same (event, trigger) -- e.g. re-running the
    same webhook transition -- must not create a second outbox row per
    channel; the UQ backstop makes the second insert attempt a no-op.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        await _create_rule(session, team, name="r1", channels=[channel])
        event = await _create_event(session, cluster, team)

        first = await route_event(session, event, "firing")
        await session.commit()
        second = await route_event(session, event, "firing")
        await session.commit()

        assert first.channels_notified == 1
        assert second.channels_notified == 0
        assert second.routed is False

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1


async def test_no_matching_rule_routes_nothing(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        await _create_rule(
            session, team, name="critical-only", channels=[channel], severities=["critical"]
        )
        event = await _create_event(session, cluster, team, severity="info")

        outcome = await route_event(session, event, "firing")

        assert outcome.routed is False
        assert outcome.reason == "no_match"


async def test_payload_is_frozen_with_app_url_and_runbook_lift(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        await _create_rule(session, team, name="r1", channels=[channel])
        event = await _create_event(
            session,
            cluster,
            team,
            annotations={"runbook_url": "https://runbooks.example.com/x", "kam_grafana_url": "https://g/d"},
        )

        await route_event(session, event, "firing")
        await session.commit()

        row = (await session.execute(select(NotificationOutbox))).scalar_one()
        payload = row.payload
        assert payload["event_id"] == event.id
        assert payload["trigger"] == "firing"
        assert payload["team_slug"] == "platform"
        assert payload["cluster"] == cluster.name
        assert payload["app_url"].endswith(f"/alerts/history/{event.id}")
        assert payload["runbook_url"] == "https://runbooks.example.com/x"
        assert payload["grafana_url"] == "https://g/d"


async def test_payload_grafana_url_falls_back_to_cluster_when_no_annotation(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = Cluster(
            name="rt-cluster-grafana",
            display_name="rt-cluster-grafana",
            prometheus_url="http://prom",
            alertmanager_url="http://am",
            grafana_url="https://cluster-grafana.example.com",
            webhook_token_hash="hash-rt-cluster-grafana",
        )
        session.add(cluster)
        await session.flush()
        channel = await _create_channel(session, team)
        await _create_rule(session, team, name="r1", channels=[channel])
        event = await _create_event(session, cluster, team, alertname="HighCpu")

        await route_event(session, event, "firing")
        await session.commit()

        row = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert row.payload["grafana_url"] == (
            "https://cluster-grafana.example.com/alerting/list?queryString=HighCpu"
        )


async def test_disabled_rule_is_not_loaded_at_all(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        await _create_rule(
            session, team, name="disabled-rule", channels=[channel], enabled=False
        )
        event = await _create_event(session, cluster, team)

        outcome = await route_event(session, event, "firing")

        assert outcome.routed is False
        assert outcome.reason == "no_match"


async def test_route_event_with_resolved_trigger(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        await _create_rule(
            session,
            team,
            name="notify-on-resolved",
            channels=[channel],
            notify_on_firing=False,
            notify_on_resolved=True,
        )
        event = await _create_event(session, cluster, team, status="resolved")

        outcome = await route_event(session, event, "resolved")
        await session.commit()

        assert outcome.routed is True
        assert outcome.channels_notified == 1

        row = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert row.trigger == "resolved"
        assert row.payload["trigger"] == "resolved"


async def test_firing_and_resolved_outbox_rows_coexist_for_same_event_channel(app) -> None:
    """The outbox UQ is on (event, channel, trigger) -- a firing routing
    pass and a later resolved routing pass for the *same* event+channel
    must both get their own row, not collide as duplicates.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        await _create_rule(
            session,
            team,
            name="notify-both",
            channels=[channel],
            notify_on_firing=True,
            notify_on_resolved=True,
        )
        event = await _create_event(session, cluster, team, status="firing")

        firing_outcome = await route_event(session, event, "firing")
        await session.commit()

        event.status = "resolved"
        resolved_outcome = await route_event(session, event, "resolved")
        await session.commit()

        assert firing_outcome.channels_notified == 1
        assert resolved_outcome.channels_notified == 1

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert {r.trigger for r in rows} == {"firing", "resolved"}
        assert len(rows) == 2
        assert all(r.channel_id == channel.id and r.alert_event_id == event.id for r in rows)


# -- Phase 14: view_notify fan-out -------------------------------------------


async def test_view_notify_share_fans_out_to_targets_include_shared_rule(app) -> None:
    async with db_module.async_session_factory() as session:
        owner = await _create_team(session, "platform")
        target = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        target_channel = await _create_channel(session, target, name="payments-email")
        await _create_rule(
            session, target, name="shared-critical", channels=[target_channel], include_shared=True
        )
        session.add(AlertShare(owner_team_id=owner.id, target_team_id=target.id, mode="view_notify"))
        await session.flush()
        event = await _create_event(session, cluster, owner, severity="critical")

        outcome = await route_event(session, event, "firing")
        await session.commit()

        # The owning team has no rules of its own -- its own outcome is
        # "no_match" regardless of the shared fan-out succeeding.
        assert outcome.reason == "no_match"

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].team_id == target.id
        assert rows[0].channel_id == target_channel.id
        assert rows[0].trigger == "firing"


async def test_view_notify_ignores_targets_rule_without_include_shared(app) -> None:
    """A target rule that would otherwise match this event must NOT fire
    for a shared-in alert unless include_shared=True -- that's the whole
    point of the gate.
    """
    async with db_module.async_session_factory() as session:
        owner = await _create_team(session, "platform")
        target = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        target_channel = await _create_channel(session, target, name="payments-email")
        await _create_rule(
            session, target, name="not-shared", channels=[target_channel], include_shared=False
        )
        session.add(AlertShare(owner_team_id=owner.id, target_team_id=target.id, mode="view_notify"))
        await session.flush()
        event = await _create_event(session, cluster, owner)

        await route_event(session, event, "firing")
        await session.commit()

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert rows == []


async def test_view_mode_share_never_notifies(app) -> None:
    """mode='view' shares only affect read visibility (app/api/alerts.py) --
    route_event's fan-out only ever considers mode='view_notify' shares.
    """
    async with db_module.async_session_factory() as session:
        owner = await _create_team(session, "platform")
        target = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        target_channel = await _create_channel(session, target, name="payments-email")
        await _create_rule(
            session, target, name="shared-critical", channels=[target_channel], include_shared=True
        )
        session.add(AlertShare(owner_team_id=owner.id, target_team_id=target.id, mode="view"))
        await session.flush()
        event = await _create_event(session, cluster, owner)

        await route_event(session, event, "firing")
        await session.commit()

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert rows == []


async def test_view_notify_matcher_scope_restricts_fan_out(app) -> None:
    async with db_module.async_session_factory() as session:
        owner = await _create_team(session, "platform")
        target = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        target_channel = await _create_channel(session, target, name="payments-email")
        await _create_rule(
            session, target, name="shared-all", channels=[target_channel], include_shared=True
        )
        session.add(
            AlertShare(
                owner_team_id=owner.id,
                target_team_id=target.id,
                mode="view_notify",
                matchers=[
                    {"kind": "include", "target": "label", "key": "severity", "pattern": "^critical$"}
                ],
            )
        )
        await session.flush()

        # Built directly (not via the shared _create_event helper, whose
        # `labels` dict never includes `severity`) since the share's
        # 'label'/'severity' matcher reads from `event.labels`, not the
        # denormalized `severity` column.
        def _make_event(*, fingerprint: str, severity: str) -> AlertEvent:
            return AlertEvent(
                cluster_id=cluster.id,
                cluster_name=cluster.name,
                fingerprint=fingerprint,
                status="firing",
                alertname="HighCpu",
                severity=severity,
                namespace="kam-demo",
                labels={"alertname": "HighCpu", "severity": severity},
                annotations={},
                team_id=owner.id,
                starts_at=datetime(2026, 1, 1, tzinfo=UTC),
            )

        info_event = _make_event(fingerprint="fp-1", severity="info")
        session.add(info_event)
        await session.flush()
        await route_event(session, info_event, "firing")
        await session.commit()
        assert (await session.execute(select(NotificationOutbox))).scalars().all() == []

        critical_event = _make_event(fingerprint="fp-2", severity="critical")
        session.add(critical_event)
        await session.flush()
        await route_event(session, critical_event, "firing")
        await session.commit()

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].alert_event_id == critical_event.id


async def test_view_notify_targets_own_suppress_blocks_only_target(app) -> None:
    """A target's own suppress rule (include_shared=True) blocks only that
    target's notification -- it must not touch the owning team's own
    routing outcome, its own notification, or event.suppressed_by_rule_id
    (which records the OWNING team's suppression history, never a
    target's).
    """
    async with db_module.async_session_factory() as session:
        owner = await _create_team(session, "platform")
        target = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        owner_channel = await _create_channel(session, owner, name="platform-email")
        await _create_rule(session, owner, name="owner-notify-all", channels=[owner_channel])
        await _create_rule(
            session,
            target,
            name="target-suppress-critical",
            action="suppress",
            include_shared=True,
            severities=["critical"],
        )
        session.add(AlertShare(owner_team_id=owner.id, target_team_id=target.id, mode="view_notify"))
        await session.flush()
        event = await _create_event(session, cluster, owner, severity="critical")

        outcome = await route_event(session, event, "firing")
        await session.commit()

        # Owner's own notification is entirely unaffected by the target's
        # suppress rule.
        assert outcome.routed is True
        assert outcome.channels_notified == 1
        assert event.suppressed_by_rule_id is None

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].team_id == owner.id
        assert rows[0].channel_id == owner_channel.id


async def test_owners_own_suppress_does_not_block_shared_fan_out(app) -> None:
    """The owning team's own suppress rule blocks only the owning team's
    own notification -- a target's view_notify fan-out (its own,
    independent routing pass) still runs.
    """
    async with db_module.async_session_factory() as session:
        owner = await _create_team(session, "platform")
        target = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        target_channel = await _create_channel(session, target, name="payments-email")
        await _create_rule(
            session, owner, name="owner-suppress-all", action="suppress"
        )
        await _create_rule(
            session, target, name="shared-critical", channels=[target_channel], include_shared=True
        )
        session.add(AlertShare(owner_team_id=owner.id, target_team_id=target.id, mode="view_notify"))
        await session.flush()
        event = await _create_event(session, cluster, owner, severity="critical")

        outcome = await route_event(session, event, "firing")
        await session.commit()

        assert outcome.reason == "suppressed"
        assert event.suppressed_by_rule_id is not None

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].team_id == target.id
        assert rows[0].channel_id == target_channel.id


async def test_view_notify_outbox_dedup_uq_still_applies_per_team(app) -> None:
    """The (event, channel, trigger) UQ dedups the shared fan-out's inserts
    the same way it dedups the owning team's own -- a repeated call for the
    same transition must not create a second shared outbox row.
    """
    async with db_module.async_session_factory() as session:
        owner = await _create_team(session, "platform")
        target = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        target_channel = await _create_channel(session, target, name="payments-email")
        await _create_rule(
            session, target, name="shared-critical", channels=[target_channel], include_shared=True
        )
        session.add(AlertShare(owner_team_id=owner.id, target_team_id=target.id, mode="view_notify"))
        await session.flush()
        event = await _create_event(session, cluster, owner)

        await route_event(session, event, "firing")
        await session.commit()
        await route_event(session, event, "firing")
        await session.commit()

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
