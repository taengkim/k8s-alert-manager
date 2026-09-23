"""Tests for the Phase 15 escalation-scheduling half of
app.services.routing.route_event (`_schedule_escalations`) and the
ack/resolve cancellation hooks (`app.services.scheduled_actions.cancel_pending`).

The dispatch side (what happens once a ScheduledAction comes due) is covered
in tests/test_scheduler.py; this file only covers route_event's own
"should a ScheduledAction get created/cancelled" decisions.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import app.db as db_module
from app.channels.email import EmailConfig
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.routing import RoutingRule
from app.models.scheduled import ScheduledAction
from app.models.team import Team
from app.security import encrypt_str
from app.services.routing import route_event


async def _create_team(session, slug: str = "platform") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(session, name: str = "esc-cluster") -> Cluster:
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


async def _create_event(
    session, cluster: Cluster, team: Team, *, fingerprint: str = "fp-1", status: str = "firing"
) -> AlertEvent:
    event = AlertEvent(
        cluster_id=cluster.id,
        cluster_name=cluster.name,
        fingerprint=fingerprint,
        status=status,
        alertname="HighCpu",
        severity="critical",
        namespace="kam-demo",
        labels={"alertname": "HighCpu"},
        annotations={},
        team_id=team.id,
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(event)
    await session.flush()
    return event


async def _pending_escalations(session, event_id: int) -> list[ScheduledAction]:
    result = await session.execute(
        select(ScheduledAction).where(
            ScheduledAction.alert_event_id == event_id,
            ScheduledAction.kind == "escalation",
            ScheduledAction.status == "pending",
        )
    )
    return list(result.scalars().all())


async def test_matched_notify_rule_with_escalation_schedules_one_action(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = RoutingRule(
            team_id=team.id,
            name="notify-escalate",
            action="notify",
            channels=[channel],
            escalation_channels=[esc_channel],
            escalation_enabled=True,
            escalation_after_minutes=10,
        )
        session.add(rule)
        await session.flush()
        event = await _create_event(session, cluster, team)

        before = datetime.now(UTC)
        await route_event(session, event, "firing")
        await session.commit()

        pending = await _pending_escalations(session, event.id)
        assert len(pending) == 1
        assert pending[0].routing_rule_id == rule.id
        assert pending[0].due_at >= before + timedelta(minutes=9, seconds=55)
        assert pending[0].due_at <= before + timedelta(minutes=10, seconds=5)


async def test_rule_without_escalation_enabled_schedules_nothing(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        rule = RoutingRule(team_id=team.id, name="notify-only", action="notify", channels=[channel])
        session.add(rule)
        await session.flush()
        event = await _create_event(session, cluster, team)

        await route_event(session, event, "firing")
        await session.commit()

        assert await _pending_escalations(session, event.id) == []


async def test_repeated_firing_transition_does_not_duplicate_escalation(app) -> None:
    """A repeated webhook delivery re-evaluating the same firing transition
    (route_event called twice) must not stack up a second pending
    escalation for the same (event, rule).
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = RoutingRule(
            team_id=team.id,
            name="notify-escalate",
            action="notify",
            channels=[channel],
            escalation_channels=[esc_channel],
            escalation_enabled=True,
            escalation_after_minutes=10,
        )
        session.add(rule)
        await session.flush()
        event = await _create_event(session, cluster, team)

        await route_event(session, event, "firing")
        await session.commit()
        await route_event(session, event, "firing")
        await session.commit()

        assert len(await _pending_escalations(session, event.id)) == 1


async def test_suppressed_event_schedules_no_escalation(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        esc_channel = await _create_channel(session, team, name="esc")
        session.add(
            RoutingRule(team_id=team.id, name="suppress-all", action="suppress", enabled=True)
        )
        session.add(
            RoutingRule(
                team_id=team.id,
                name="notify-escalate",
                action="notify",
                channels=[channel],
                escalation_channels=[esc_channel],
                escalation_enabled=True,
                escalation_after_minutes=10,
            )
        )
        await session.flush()
        event = await _create_event(session, cluster, team)

        outcome = await route_event(session, event, "firing")
        await session.commit()

        assert outcome.reason == "suppressed"
        assert await _pending_escalations(session, event.id) == []


async def test_resolved_trigger_schedules_no_escalation_even_if_notify_on_resolved(app) -> None:
    """Escalation means "still unresolved after N minutes" -- meaningless
    for a resolved transition, even for a rule with notify_on_resolved=True
    and escalation configured.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = RoutingRule(
            team_id=team.id,
            name="notify-resolved-escalate",
            action="notify",
            channels=[channel],
            escalation_channels=[esc_channel],
            escalation_enabled=True,
            escalation_after_minutes=10,
            notify_on_firing=False,
            notify_on_resolved=True,
        )
        session.add(rule)
        await session.flush()
        event = await _create_event(session, cluster, team, status="resolved")

        await route_event(session, event, "resolved")
        await session.commit()

        assert await _pending_escalations(session, event.id) == []


async def test_resolved_transition_cancels_pending_escalation_and_renotify(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        rule = RoutingRule(team_id=team.id, name="r1", action="notify", channels=[channel])
        session.add(rule)
        await session.flush()
        event = await _create_event(session, cluster, team)
        session.add(
            ScheduledAction(
                kind="escalation",
                alert_event_id=event.id,
                routing_rule_id=rule.id,
                due_at=datetime.now(UTC) + timedelta(minutes=5),
                status="pending",
            )
        )
        session.add(
            ScheduledAction(
                kind="renotify",
                alert_event_id=event.id,
                routing_rule_id=rule.id,
                due_at=datetime.now(UTC) + timedelta(minutes=15),
                status="pending",
            )
        )
        await session.commit()

        event.status = "resolved"
        await route_event(session, event, "resolved")
        await session.commit()

        remaining_pending = (
            await session.execute(
                select(ScheduledAction).where(
                    ScheduledAction.alert_event_id == event.id,
                    ScheduledAction.status == "pending",
                )
            )
        ).scalars().all()
        assert remaining_pending == []

        cancelled = (
            await session.execute(
                select(ScheduledAction).where(
                    ScheduledAction.alert_event_id == event.id,
                    ScheduledAction.status == "cancelled",
                )
            )
        ).scalars().all()
        assert len(cancelled) == 2
        assert all(a.processed_at is not None for a in cancelled)
