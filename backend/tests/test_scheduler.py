"""Tests for app/worker/scheduler.py: the ScheduledAction claim/dispatch/
lease-recovery lifecycle, and the escalation/renotify dispatch handlers.

Only the SQLite claim_due_actions branch is exercised here -- same posture
as tests/test_outbox_worker.py (see that file's docstring): the Postgres
`FOR UPDATE SKIP LOCKED` branch isn't covered by an automated test in this
repo.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from sqlalchemy import select

import app.db as db_module
from app.channels.email import EmailConfig
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.scheduled import ScheduledAction
from app.models.team import Team
from app.security import encrypt_str
from app.worker.scheduler import (
    claim_due_actions,
    dispatch,
    recover_stale_claims,
    run_scheduler_tick,
    schedule_renotify,
)


async def _create_team(session, slug: str = "platform") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(session, name: str = "sched-cluster") -> Cluster:
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


async def _create_rule(
    session, team: Team, *, name: str = "r1", channels=None, escalation_channels=None, **fields
) -> RoutingRule:
    # channels/escalation_channels are passed to the constructor (not
    # assigned after the fact) -- assigning a collection relationship on an
    # already-flushed-but-uncommitted object forces SQLAlchemy to lazy-load
    # its prior value to diff against, which needs an awaited context this
    # sync assignment doesn't provide (MissingGreenlet).
    rule = RoutingRule(
        team_id=team.id,
        name=name,
        action="notify",
        channels=channels or [],
        escalation_channels=escalation_channels or [],
        **fields,
    )
    session.add(rule)
    await session.flush()
    return rule


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


async def _create_action(
    session,
    *,
    kind: str,
    event: AlertEvent | None,
    rule: RoutingRule | None,
    due_at: datetime,
    status: str = "pending",
    processed_at: datetime | None = None,
) -> ScheduledAction:
    action = ScheduledAction(
        kind=kind,
        alert_event_id=event.id if event else None,
        routing_rule_id=rule.id if rule else None,
        due_at=due_at,
        status=status,
        processed_at=processed_at,
    )
    session.add(action)
    await session.flush()
    return action


# -- claim / recovery --------------------------------------------------------


async def test_claim_due_actions_claims_only_due_pending_rows(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        event = await _create_event(session, cluster, team)
        rule = await _create_rule(session, team)
        now = datetime.now(UTC)

        due = await _create_action(session, kind="escalation", event=event, rule=rule, due_at=now - timedelta(minutes=1))
        not_due = await _create_action(session, kind="escalation", event=event, rule=rule, due_at=now + timedelta(hours=1))
        already_done = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=now - timedelta(minutes=1), status="done"
        )
        await session.commit()

        claimed = await claim_due_actions(session, "worker-1", limit=50)
        claimed_ids = {a.id for a in claimed}

        assert claimed_ids == {due.id}
        assert not_due.id not in claimed_ids
        assert already_done.id not in claimed_ids

        await session.refresh(due)
        assert due.status == "claimed"


async def test_claim_resets_due_at_so_overdue_action_is_not_immediately_recoverable(app) -> None:
    """A `ScheduledAction` that was significantly overdue when claimed (e.g.
    after the worker was down for a while) must not immediately look like
    an abandoned claim to `recover_stale_claims` -- `claim_due_actions`
    resets `due_at` to the claim time itself, since that's this module's
    lease clock (see the module docstring).
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        event = await _create_event(session, cluster, team)
        rule = await _create_rule(session, team)
        very_overdue = datetime.now(UTC) - timedelta(minutes=10)
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=very_overdue
        )
        await session.commit()

        claimed = await claim_due_actions(session, "worker-1", limit=50)
        assert len(claimed) == 1
        assert claimed[0].due_at > very_overdue + timedelta(minutes=9)

        recovered = await recover_stale_claims(session, lease_timeout=timedelta(minutes=5))
        assert recovered == 0

        await session.refresh(action)
        assert action.status == "claimed"


async def test_recover_stale_claims_restores_abandoned_claim(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        event = await _create_event(session, cluster, team)
        rule = await _create_rule(session, team)
        now = datetime.now(UTC)

        stale = await _create_action(
            session, kind="escalation", event=event, rule=rule,
            due_at=now - timedelta(minutes=10), status="claimed",
        )
        fresh = await _create_action(
            session, kind="escalation", event=event, rule=rule,
            due_at=now - timedelta(minutes=1), status="claimed",
        )
        already_processed = await _create_action(
            session, kind="escalation", event=event, rule=rule,
            due_at=now - timedelta(minutes=10), status="claimed", processed_at=now,
        )
        await session.commit()

        recovered = await recover_stale_claims(session, lease_timeout=timedelta(minutes=5))
        assert recovered == 1

        await session.refresh(stale)
        await session.refresh(fresh)
        await session.refresh(already_processed)
        assert stale.status == "pending"
        assert fresh.status == "claimed"
        assert already_processed.status == "claimed"


# -- dispatch: unknown kind ---------------------------------------------------


async def test_dispatch_unknown_kind_marks_done_not_retried(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        event = await _create_event(session, cluster, team)
        action = await _create_action(
            session, kind="deadman", event=event, rule=None, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "done"
        assert action.processed_at is not None


# -- dispatch: escalation -----------------------------------------------------


async def test_dispatch_escalation_stages_outbox_and_settles_done(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = await _create_rule(
            session, team, escalation_enabled=True, escalation_after_minutes=5,
            escalation_channels=[esc_channel],
        )
        event = await _create_event(session, cluster, team)
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "done"
        assert action.processed_at is not None
        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].trigger == "escalation"
        assert rows[0].channel_id == esc_channel.id
        assert rows[0].alert_event_id == event.id


async def test_dispatch_escalation_cancelled_when_acknowledged(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = await _create_rule(
            session, team, escalation_enabled=True, escalation_after_minutes=5,
            escalation_channels=[esc_channel],
        )
        event = await _create_event(session, cluster, team)
        event.acknowledged_at = datetime.now(UTC)
        await session.flush()
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "cancelled"
        assert (await session.execute(select(NotificationOutbox))).scalars().all() == []


async def test_dispatch_escalation_cancelled_when_resolved(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = await _create_rule(
            session, team, escalation_enabled=True, escalation_after_minutes=5,
            escalation_channels=[esc_channel],
        )
        event = await _create_event(session, cluster, team, status="resolved")
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "cancelled"
        assert (await session.execute(select(NotificationOutbox))).scalars().all() == []


async def test_dispatch_escalation_cancelled_when_no_escalation_channels(app) -> None:
    """The rule's escalation_channels emptied out (all deleted, or never
    set) after the ScheduledAction was scheduled -- nothing left to notify.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        rule = await _create_rule(session, team, escalation_enabled=True, escalation_after_minutes=5)
        event = await _create_event(session, cluster, team)
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "cancelled"


async def test_dispatch_escalation_skips_channel_with_revoked_cross_team_consent(app) -> None:
    """Defense in depth (I2a): even if a stale join row survives (e.g. it
    predates app/api/channels.py's update_channel cleanup, or a direct DB
    edit), dispatch itself re-checks allow_cross_team_escalation and never
    delivers to a channel that's no longer allowed -- it just skips that
    one channel rather than cancelling the whole escalation, as long as at
    least one other channel is still allowed.
    """
    async with db_module.async_session_factory() as session:
        owner_team = await _create_team(session, "owner-team")
        other_team = await _create_team(session, "other-team")
        cluster = await _create_cluster(session)
        own_channel = await _create_channel(session, owner_team, name="own")
        foreign_channel = await _create_channel(session, other_team, name="foreign")
        # Simulates a rule that selected `foreign_channel` while it still
        # allowed cross-team escalation, which has since been revoked --
        # the join row itself is untouched here (that's exactly the stale
        # state this defense-in-depth check guards against).
        foreign_channel.allow_cross_team_escalation = False
        rule = await _create_rule(
            session,
            owner_team,
            escalation_enabled=True,
            escalation_after_minutes=5,
            escalation_channels=[own_channel, foreign_channel],
        )
        event = await _create_event(session, cluster, owner_team)
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "done"
        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].channel_id == own_channel.id


async def test_dispatch_escalation_cancelled_when_all_channels_revoked(app) -> None:
    async with db_module.async_session_factory() as session:
        owner_team = await _create_team(session, "owner-team-2")
        other_team = await _create_team(session, "other-team-2")
        cluster = await _create_cluster(session)
        foreign_channel = await _create_channel(session, other_team, name="foreign-2")
        foreign_channel.allow_cross_team_escalation = False
        rule = await _create_rule(
            session,
            owner_team,
            escalation_enabled=True,
            escalation_after_minutes=5,
            escalation_channels=[foreign_channel],
        )
        event = await _create_event(session, cluster, owner_team)
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "cancelled"
        assert (await session.execute(select(NotificationOutbox))).scalars().all() == []


# -- dispatch: renotify --------------------------------------------------------


async def test_dispatch_renotify_stages_outbox_and_reschedules(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        rule = await _create_rule(session, team, channels=[channel], renotify_interval_minutes=15)
        event = await _create_event(session, cluster, team)
        action = await _create_action(
            session, kind="renotify", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)
        await session.commit()

        assert action.status == "done"
        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].trigger == f"renotify:{action.id}"
        assert rows[0].channel_id == channel.id

        rescheduled = (
            await session.execute(
                select(ScheduledAction).where(
                    ScheduledAction.kind == "renotify", ScheduledAction.status == "pending"
                )
            )
        ).scalars().all()
        assert len(rescheduled) == 1
        assert rescheduled[0].id != action.id
        assert rescheduled[0].due_at > datetime.now(UTC) + timedelta(minutes=14)


async def test_dispatch_renotify_two_cycles_both_deliver(app) -> None:
    """The trigger must differ per cycle (f'renotify:{action.id}') -- a bare
    'renotify' reused every cycle would collide with the first cycle's row
    on the outbox's (event, channel, trigger) unique constraint and silently
    stop delivering after the first ping.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        rule = await _create_rule(session, team, channels=[channel], renotify_interval_minutes=15)
        event = await _create_event(session, cluster, team)
        first_action = await _create_action(
            session, kind="renotify", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(first_action, session)
        await session.commit()

        second_action = (
            await session.execute(
                select(ScheduledAction).where(
                    ScheduledAction.kind == "renotify", ScheduledAction.status == "pending"
                )
            )
        ).scalar_one()
        second_action.status = "claimed"
        await session.commit()

        await dispatch(second_action, session)
        await session.commit()

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 2
        assert {r.trigger for r in rows} == {
            f"renotify:{first_action.id}",
            f"renotify:{second_action.id}",
        }


async def test_dispatch_renotify_cancelled_when_acknowledged_no_reschedule(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        rule = await _create_rule(session, team, channels=[channel], renotify_interval_minutes=15)
        event = await _create_event(session, cluster, team)
        event.acknowledged_at = datetime.now(UTC)
        await session.flush()
        action = await _create_action(
            session, kind="renotify", event=event, rule=rule, due_at=datetime.now(UTC), status="claimed"
        )
        await session.commit()

        await dispatch(action, session)

        assert action.status == "cancelled"
        assert (await session.execute(select(NotificationOutbox))).scalars().all() == []
        assert (
            await session.execute(select(ScheduledAction).where(ScheduledAction.status == "pending"))
        ).scalars().all() == []


async def test_schedule_renotify_guarantees_single_pending(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        rule = await _create_rule(session, team, channels=[channel], renotify_interval_minutes=15)
        event = await _create_event(session, cluster, team)
        await session.commit()

        await schedule_renotify(session, event.id, rule)
        await schedule_renotify(session, event.id, rule)
        await session.commit()

        pending = (
            await session.execute(
                select(ScheduledAction).where(
                    ScheduledAction.alert_event_id == event.id,
                    ScheduledAction.kind == "renotify",
                    ScheduledAction.status == "pending",
                )
            )
        ).scalars().all()
        assert len(pending) == 1


# -- dispatch: retry on unexpected failure -------------------------------------


async def test_dispatch_retries_on_unexpected_exception(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = await _create_rule(
            session, team, escalation_enabled=True, escalation_after_minutes=5,
            escalation_channels=[esc_channel],
        )
        event = await _create_event(session, cluster, team)
        due_at = datetime.now(UTC) - timedelta(minutes=1)
        action = await _create_action(
            session, kind="escalation", event=event, rule=rule, due_at=due_at, status="claimed"
        )
        await session.commit()

        with patch(
            "app.worker.scheduler.build_notification_for_event", side_effect=RuntimeError("boom")
        ):
            await dispatch(action, session)

        assert action.status == "pending"
        assert action.due_at > due_at
        assert action.processed_at is None


# -- run_scheduler_tick (integration) ------------------------------------------


async def test_run_scheduler_tick_claims_and_dispatches(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        esc_channel = await _create_channel(session, team, name="esc")
        rule = await _create_rule(
            session, team, escalation_enabled=True, escalation_after_minutes=5,
            escalation_channels=[esc_channel],
        )
        event = await _create_event(session, cluster, team)
        await _create_action(
            session, kind="escalation", event=event, rule=rule,
            due_at=datetime.now(UTC) - timedelta(minutes=1),
        )
        await session.commit()

    processed = await run_scheduler_tick(db_module.async_session_factory, "worker-1")
    assert processed == 1

    async with db_module.async_session_factory() as session:
        action = (await session.execute(select(ScheduledAction))).scalar_one()
        assert action.status == "done"
        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        assert rows[0].trigger == "escalation"
