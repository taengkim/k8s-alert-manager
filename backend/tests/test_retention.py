"""Tests for app.services.retention.purge: window boundaries, the
firing-events-never-purged invariant, batching, and the summary/audit/
last_purge_at bookkeeping. app/worker/scheduler.py's daily-sweep gating
(`maybe_run_retention_sweep`) is covered at the bottom.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.audit import AuditLog
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.scheduled import ScheduledAction
from app.models.team import Team
from app.security import encrypt_str
from app.services.retention import purge
from app.services.settings import get_last_purge_at
from app.worker.scheduler import maybe_run_retention_sweep

NOW = datetime.now(UTC)


async def _create_team(session, slug: str = "platform") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(session, name: str = "ret-cluster") -> Cluster:
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
        config_encrypted=encrypt_str('{"recipients": ["ops@example.org"]}'),
    )
    session.add(channel)
    await session.flush()
    return channel


def _make_event(
    cluster: Cluster,
    team: Team,
    *,
    fingerprint: str,
    status: str = "resolved",
    is_test: bool = False,
    last_received_at: datetime,
) -> AlertEvent:
    return AlertEvent(
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
        starts_at=last_received_at - timedelta(minutes=5),
        ends_at=last_received_at if status == "resolved" else None,
        first_received_at=last_received_at,
        last_received_at=last_received_at,
        is_test=is_test,
    )


async def _all(session, model, **filters):
    stmt = select(model)
    for key, value in filters.items():
        stmt = stmt.where(getattr(model, key) == value)
    return (await session.execute(stmt)).scalars().all()


# -- alert_events --------------------------------------------------------


async def test_resolved_event_boundary_89d_kept_91d_purged(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        kept = _make_event(
            cluster, team, fingerprint="kept",
            last_received_at=NOW - timedelta(days=89),
        )
        purged = _make_event(
            cluster, team, fingerprint="purged",
            last_received_at=NOW - timedelta(days=91),
        )
        session.add_all([kept, purged])
        await session.commit()
        kept_id, purged_id = kept.id, purged.id

    summary = await purge(db_module.async_session_factory)
    assert summary["alert_events"] == 1

    async with db_module.async_session_factory() as session:
        assert await session.get(AlertEvent, kept_id) is not None
        assert await session.get(AlertEvent, purged_id) is None


async def test_firing_event_never_purged_regardless_of_age(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        ancient_firing = _make_event(
            cluster, team, fingerprint="ancient-firing", status="firing",
            last_received_at=NOW - timedelta(days=3650),
        )
        session.add(ancient_firing)
        await session.commit()
        event_id = ancient_firing.id

    await purge(db_module.async_session_factory)

    async with db_module.async_session_factory() as session:
        assert await session.get(AlertEvent, event_id) is not None


async def test_test_alert_purged_after_7_days_regardless_of_firing_status(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        old_test_firing = _make_event(
            cluster, team, fingerprint="old-test-firing", status="firing", is_test=True,
            last_received_at=NOW - timedelta(days=8),
        )
        recent_test_firing = _make_event(
            cluster, team, fingerprint="recent-test-firing", status="firing", is_test=True,
            last_received_at=NOW - timedelta(days=3),
        )
        session.add_all([old_test_firing, recent_test_firing])
        await session.commit()
        old_id, recent_id = old_test_firing.id, recent_test_firing.id

    summary = await purge(db_module.async_session_factory)
    assert summary["alert_events"] == 1

    async with db_module.async_session_factory() as session:
        assert await session.get(AlertEvent, old_id) is None
        assert await session.get(AlertEvent, recent_id) is not None


async def test_purging_event_force_deletes_its_own_outbox_rows_regardless_of_age(app) -> None:
    """NotificationOutbox.alert_event_id has no ON DELETE clause -- a purge-
    eligible event's outbox rows must be force-deleted first (regardless of
    their own 30-day window) or the event delete would violate that FK.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        event = _make_event(
            cluster, team, fingerprint="fp-1",
            last_received_at=NOW - timedelta(days=91),
        )
        session.add(event)
        await session.flush()
        # A brand-new (created_at=now), still-'pending' outbox row -- nowhere
        # near notification_outbox's own 30-day window on its own.
        session.add(
            NotificationOutbox(
                alert_event_id=event.id,
                channel_id=channel.id,
                team_id=team.id,
                trigger="resolved",
                payload={},
                status="pending",
            )
        )
        await session.commit()
        event_id = event.id

    summary = await purge(db_module.async_session_factory)
    assert summary["alert_events"] == 1

    async with db_module.async_session_factory() as session:
        assert await session.get(AlertEvent, event_id) is None
        assert await _all(session, NotificationOutbox, alert_event_id=event_id) == []


# -- notification_outbox --------------------------------------------------


async def test_notification_outbox_terminal_rows_purged_after_30_days(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        event = _make_event(cluster, team, fingerprint="fp-1", last_received_at=NOW)
        session.add(event)
        await session.flush()

        old_delivered = NotificationOutbox(
            alert_event_id=event.id, channel_id=channel.id, team_id=team.id,
            trigger="firing", payload={}, status="delivered",
            created_at=NOW - timedelta(days=31),
        )
        recent_delivered = NotificationOutbox(
            alert_event_id=event.id, channel_id=channel.id, team_id=team.id,
            trigger="resolved", payload={}, status="delivered",
            created_at=NOW - timedelta(days=1),
        )
        old_pending = NotificationOutbox(
            alert_event_id=event.id, channel_id=channel.id, team_id=team.id,
            trigger="escalation", payload={}, status="pending",
            created_at=NOW - timedelta(days=31),
        )
        session.add_all([old_delivered, recent_delivered, old_pending])
        await session.commit()
        old_delivered_id, recent_id, old_pending_id = (
            old_delivered.id, recent_delivered.id, old_pending.id,
        )

    summary = await purge(db_module.async_session_factory)
    assert summary["notification_outbox"] == 1

    async with db_module.async_session_factory() as session:
        assert await session.get(NotificationOutbox, old_delivered_id) is None
        assert await session.get(NotificationOutbox, recent_id) is not None
        # A 'pending' row is never purged by age alone, regardless of how
        # old -- only a terminal (delivered/dead) row qualifies.
        assert await session.get(NotificationOutbox, old_pending_id) is not None


# -- notification_outbox: Phase 16 digest aggregate + parked children -------


async def test_purging_old_digest_aggregate_takes_its_parked_children_with_it(app) -> None:
    """A parked ('digested') row records no delivery history of its own once
    linked to an aggregate -- when the aggregate becomes purge-eligible
    (delivered/dead + past the window), its children must be force-deleted
    in the same sweep, regardless of THEIR OWN age/status, or they'd survive
    as orphans (digested_into_id SET NULL) indistinguishable from a row
    still awaiting a flush.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        # Two distinct events -- a digest bundles rows across DIFFERENT
        # events for one channel, so two rows sharing one event/channel/
        # trigger would just collide on the (alert_event_id, channel_id,
        # trigger) UQ, which has nothing to do with what's under test here.
        event_1 = _make_event(cluster, team, fingerprint="fp-1", last_received_at=NOW)
        event_2 = _make_event(cluster, team, fingerprint="fp-2", last_received_at=NOW)
        session.add_all([event_1, event_2])
        await session.flush()

        old_aggregate = NotificationOutbox(
            alert_event_id=None, channel_id=channel.id, team_id=team.id,
            trigger="digest", payload={"notifications": [], "count": 2, "window_started_at": "x"},
            is_digest=True, status="delivered", created_at=NOW - timedelta(days=31),
        )
        session.add(old_aggregate)
        await session.flush()
        # Both children are brand-new (created_at=now) and would never
        # qualify for the general age-based purge on their own.
        child_1 = NotificationOutbox(
            alert_event_id=event_1.id, channel_id=channel.id, team_id=team.id,
            trigger="firing", payload={}, status="digested",
            digested_into_id=old_aggregate.id, created_at=NOW,
        )
        child_2 = NotificationOutbox(
            alert_event_id=event_2.id, channel_id=channel.id, team_id=team.id,
            trigger="firing", payload={}, status="digested",
            digested_into_id=old_aggregate.id, created_at=NOW,
        )
        session.add_all([child_1, child_2])
        await session.commit()
        aggregate_id, child_1_id, child_2_id = old_aggregate.id, child_1.id, child_2.id

    summary = await purge(db_module.async_session_factory)
    # 1 aggregate + 2 children = 3 notification_outbox rows removed.
    assert summary["notification_outbox"] == 3

    async with db_module.async_session_factory() as session:
        assert await session.get(NotificationOutbox, aggregate_id) is None
        assert await session.get(NotificationOutbox, child_1_id) is None
        assert await session.get(NotificationOutbox, child_2_id) is None


async def test_recent_digest_aggregate_keeps_its_parked_children_intact(app) -> None:
    """An aggregate younger than the retention window is never purged, so
    its children (however old) must be untouched too -- the children-follow-
    aggregate rule only fires once the aggregate itself is purge-eligible.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        event = _make_event(cluster, team, fingerprint="fp-1", last_received_at=NOW)
        session.add(event)
        await session.flush()

        recent_aggregate = NotificationOutbox(
            alert_event_id=None, channel_id=channel.id, team_id=team.id,
            trigger="digest", payload={"notifications": [], "count": 1, "window_started_at": "x"},
            is_digest=True, status="delivered", created_at=NOW - timedelta(days=1),
        )
        session.add(recent_aggregate)
        await session.flush()
        # An old child would normally be exempt anyway (its own status is
        # 'digested', not 'delivered'/'dead'), but backdating it here proves
        # the aggregate's own (recent) age is what actually gates this,
        # not the child's.
        child = NotificationOutbox(
            alert_event_id=event.id, channel_id=channel.id, team_id=team.id,
            trigger="firing", payload={}, status="digested",
            digested_into_id=recent_aggregate.id, created_at=NOW - timedelta(days=60),
        )
        session.add(child)
        await session.commit()
        aggregate_id, child_id = recent_aggregate.id, child.id

    summary = await purge(db_module.async_session_factory)
    assert summary["notification_outbox"] == 0

    async with db_module.async_session_factory() as session:
        assert await session.get(NotificationOutbox, aggregate_id) is not None
        assert await session.get(NotificationOutbox, child_id) is not None


async def test_parked_child_purged_with_its_own_event_before_aggregate_ages_out(app) -> None:
    """The other direction: a parked row's OWNING EVENT purges first (the
    aggregate is still recent) -- `_purge_alert_events` force-deletes every
    outbox row for that event regardless of status, same as always, and the
    (still-recent, unrelated) aggregate is left alone.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)
        old_event = _make_event(
            cluster, team, fingerprint="fp-old", last_received_at=NOW - timedelta(days=91)
        )
        session.add(old_event)
        await session.flush()

        aggregate = NotificationOutbox(
            alert_event_id=None, channel_id=channel.id, team_id=team.id,
            trigger="digest", payload={"notifications": [], "count": 1, "window_started_at": "x"},
            is_digest=True, status="delivered", created_at=NOW,
        )
        session.add(aggregate)
        await session.flush()
        child = NotificationOutbox(
            alert_event_id=old_event.id, channel_id=channel.id, team_id=team.id,
            trigger="firing", payload={}, status="digested",
            digested_into_id=aggregate.id, created_at=NOW,
        )
        session.add(child)
        await session.commit()
        event_id, aggregate_id, child_id = old_event.id, aggregate.id, child.id

    summary = await purge(db_module.async_session_factory)
    assert summary["alert_events"] == 1

    async with db_module.async_session_factory() as session:
        assert await session.get(AlertEvent, event_id) is None
        assert await session.get(NotificationOutbox, child_id) is None
        # The aggregate itself is untouched -- it's recent, and purging the
        # event it was (partly) built from never reaches back into it.
        assert await session.get(NotificationOutbox, aggregate_id) is not None


# -- audit_log -------------------------------------------------------------


async def test_audit_log_purged_after_365_days(app) -> None:
    async with db_module.async_session_factory() as session:
        old = AuditLog(
            user_id=None, team_id=None, action="test.old", object_type="x", object_ref="1",
            created_at=NOW - timedelta(days=366),
        )
        recent = AuditLog(
            user_id=None, team_id=None, action="test.recent", object_type="x", object_ref="2",
            created_at=NOW - timedelta(days=1),
        )
        session.add_all([old, recent])
        await session.commit()
        old_id, recent_id = old.id, recent.id

    summary = await purge(db_module.async_session_factory)
    assert summary["audit_logs"] == 1

    async with db_module.async_session_factory() as session:
        assert await session.get(AuditLog, old_id) is None
        assert await session.get(AuditLog, recent_id) is not None


# -- scheduled_actions -------------------------------------------------------


async def test_scheduled_actions_terminal_rows_purged_after_7_days(app) -> None:
    async with db_module.async_session_factory() as session:
        old_done = ScheduledAction(
            kind="escalation", due_at=NOW - timedelta(days=10), status="done",
            processed_at=NOW - timedelta(days=8),
        )
        recent_cancelled = ScheduledAction(
            kind="renotify", due_at=NOW - timedelta(days=1), status="cancelled",
            processed_at=NOW - timedelta(days=1),
        )
        still_pending = ScheduledAction(
            kind="escalation", due_at=NOW - timedelta(days=10), status="pending",
        )
        session.add_all([old_done, recent_cancelled, still_pending])
        await session.commit()
        old_id, recent_id, pending_id = old_done.id, recent_cancelled.id, still_pending.id

    summary = await purge(db_module.async_session_factory)
    assert summary["scheduled_actions"] == 1

    async with db_module.async_session_factory() as session:
        assert await session.get(ScheduledAction, old_id) is None
        assert await session.get(ScheduledAction, recent_id) is not None
        assert await session.get(ScheduledAction, pending_id) is not None


# -- batching ----------------------------------------------------------------


async def test_batches_of_2500_rows_purged_across_multiple_1000_row_batches(app) -> None:
    async with db_module.async_session_factory() as session:
        old_cutoff = NOW - timedelta(days=366)
        session.add_all(
            [
                AuditLog(
                    user_id=None, team_id=None, action="bulk", object_type="x",
                    object_ref=str(i), created_at=old_cutoff,
                )
                for i in range(2500)
            ]
        )
        await session.commit()

    summary = await purge(db_module.async_session_factory)
    assert summary["audit_logs"] == 2500

    async with db_module.async_session_factory() as session:
        remaining = (
            await session.execute(
                select(func.count()).select_from(AuditLog).where(AuditLog.action == "bulk")
            )
        ).scalar_one()
        assert remaining == 0


# -- summary / audit / settings ----------------------------------------------


async def test_purge_writes_audit_log_and_updates_last_purge_at(app) -> None:
    async with db_module.async_session_factory() as session:
        old = AuditLog(
            user_id=None, team_id=None, action="test.old", object_type="x", object_ref="1",
            created_at=NOW - timedelta(days=366),
        )
        session.add(old)
        await session.commit()

    before = datetime.now(UTC)
    summary = await purge(db_module.async_session_factory, actor_user_id=None)
    assert summary == {
        "alert_events": 0,
        "notification_outbox": 0,
        "scheduled_actions": 0,
        "audit_logs": 1,
    }

    async with db_module.async_session_factory() as session:
        purge_audit_rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "retention.purge"))
        ).scalars().all()
        assert len(purge_audit_rows) == 1
        assert purge_audit_rows[0].detail == summary

        last_purge_at = await get_last_purge_at(session)
        assert last_purge_at is not None
        assert last_purge_at >= before


async def test_settings_override_widens_retention_window(app) -> None:
    from app.services.settings import set_setting

    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        event = _make_event(
            cluster, team, fingerprint="fp-1", last_received_at=NOW - timedelta(days=95)
        )
        session.add(event)
        await set_setting(session, "retention.alert_events_days", "120")
        await session.commit()
        event_id = event.id

    await purge(db_module.async_session_factory)

    async with db_module.async_session_factory() as session:
        # 95 days old, but the (overridden) window is now 120 days -- kept.
        assert await session.get(AlertEvent, event_id) is not None


# -- scheduler daily-sweep gating ---------------------------------------------


async def test_maybe_run_retention_sweep_runs_when_never_run_before(app) -> None:
    async with db_module.async_session_factory() as session:
        old = AuditLog(
            user_id=None, team_id=None, action="test.old", object_type="x", object_ref="1",
            created_at=NOW - timedelta(days=366),
        )
        session.add(old)
        await session.commit()

    summary = await maybe_run_retention_sweep(db_module.async_session_factory)
    assert summary is not None
    assert summary["audit_logs"] == 1


async def test_maybe_run_retention_sweep_skips_within_24h_of_last_run(app) -> None:
    from app.services.settings import set_last_purge_at

    async with db_module.async_session_factory() as session:
        await set_last_purge_at(session, NOW - timedelta(hours=1))
        await session.commit()

    summary = await maybe_run_retention_sweep(db_module.async_session_factory)
    assert summary is None


async def test_maybe_run_retention_sweep_runs_after_24h_restart_safe(app) -> None:
    """Restart-safe: the gate is the stored 'retention.last_purge_at'
    AppSetting, not an in-process timer -- a fresh process still respects a
    purge that happened (per the DB) more than 24h ago.
    """
    from app.services.settings import set_last_purge_at

    async with db_module.async_session_factory() as session:
        await set_last_purge_at(session, NOW - timedelta(hours=25))
        await session.commit()

    summary = await maybe_run_retention_sweep(db_module.async_session_factory)
    assert summary is not None


# -- get_int_setting: non-positive stored values fall back to default -------


async def test_get_int_setting_falls_back_to_default_for_zero_or_negative(app) -> None:
    """A stored `0` or negative value must not silently become "purge
    everything older than negative-N days" -- get_int_setting treats it the
    same as an unset/malformed value: fall back to `default`.
    """
    from app.services.settings import get_int_setting, set_setting

    async with db_module.async_session_factory() as session:
        await set_setting(session, "retention.alert_events_days", "0")
        await session.commit()
        assert await get_int_setting(session, "retention.alert_events_days", 90) == 90

        await set_setting(session, "retention.alert_events_days", "-5")
        await session.commit()
        assert await get_int_setting(session, "retention.alert_events_days", 90) == 90

        await set_setting(session, "retention.alert_events_days", "30")
        await session.commit()
        assert await get_int_setting(session, "retention.alert_events_days", 90) == 30
