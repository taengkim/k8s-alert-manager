"""Tests for app.services.routing.route_event: the DB-touching half of the
routing engine (compile_rule/evaluate are covered purely in
test_routing_engine.py). Exercises suppress-first precedence, multi-rule
channel-union dedup via the outbox UQ, the unassigned-team skip, and the
frozen notification payload's content.
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
