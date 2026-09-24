"""The scheduled-action worker: claims and dispatches `ScheduledAction` rows
staged by `app.services.routing.route_event` (kind='escalation'),
`app/worker/outbox.py`'s `deliver()` (kind='renotify'), and (Phase 16)
`app.services.routing.stage_outbox_row` (kind='digest_flush', once a
channel's storm control parks a notification), plus the daily retention
purge sweep (`app.services.retention.purge`).

Deliberately FastAPI-free, same reasoning as `app/worker/outbox.py`: this
runs embedded in the API process's lifespan (see `app/main.py`) via
`app/worker/outbox.py`'s `run_loop`, which ticks this module's
`run_scheduler_tick`/`maybe_run_retention_sweep` alongside its own outbox
polling on their own, longer intervals -- one background task, not two,
per this phase's brief (documented there as the simpler of the two options
considered: a second independent loop would need its own task, its own
stop-event handling, and its own lifespan wiring for no real benefit over
piggybacking on the loop that already exists).

Claim lifecycle -- deliberately NOT the same vocabulary as
`app/worker/outbox.py`'s claim_batch (`'pending' -> 'in_progress' ->
'delivered'/'dead'`, with `locked_by`/`locked_at` columns for lease
tracking): `ScheduledAction` has no locked_by/locked_at of its own, so this
module uses `'pending' -> 'claimed' -> 'done'/'cancelled'` instead, and
recovers an abandoned claim (a worker that claimed a row but crashed before
settling it) by treating a `'claimed'` row whose `due_at` is more than
`DEFAULT_LEASE_TIMEOUT` in the past as stale -- `due_at` doubles as the
lease clock here since there's no separate locked_at to use, which is
simple enough given how rarely a claim should ever be abandoned in the
first place. A dispatch that raises restores 'pending' with `due_at` pushed
back `RETRY_BACKOFF` rather than retrying immediately in a tight loop.
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.models.report import ReportSchedule
from app.models.routing import RoutingRule
from app.models.scheduled import ScheduledAction
from app.services import reports as reports_service
from app.services.retention import purge
from app.services.routing import build_notification_for_event, stage_outbox_row
from app.services.settings import get_last_purge_at

logger = logging.getLogger(__name__)

RETRY_BACKOFF = timedelta(minutes=1)
DEFAULT_LEASE_TIMEOUT = timedelta(minutes=5)
RETENTION_SWEEP_INTERVAL = timedelta(hours=24)
# Phase 20: how far claim_due_reports provisionally pushes a claimed
# schedule's next_run_at forward -- see that function's docstring.
REPORT_CLAIM_BUMP = timedelta(hours=1)


class _Skip(Exception):
    """Raised internally by a dispatch handler to mean "there is nothing
    left to act on -- settle this action as 'cancelled', not an error".
    Never escapes `dispatch()`.
    """


async def claim_due_actions(
    session: AsyncSession, worker_id: str, limit: int = 50
) -> list[ScheduledAction]:
    """Atomically claim up to `limit` due 'pending' rows, marking them
    'claimed'. Dialect-branched exactly like `app.worker.outbox.claim_batch`
    -- see that function's docstring for why (Postgres `FOR UPDATE SKIP
    LOCKED`; SQLite single-process select-then-update).

    `worker_id` isn't persisted anywhere (`ScheduledAction` has no
    locked_by column -- see this module's docstring) -- it's accepted only
    for signature symmetry with `claim_batch` and folded into this
    function's own log lines, should claiming ever need to be debugged
    across multiple worker processes.

    Resets `due_at` to `now` as part of the claim itself: `due_at` is this
    module's lease clock (see the module docstring), and a row that was
    significantly overdue when claimed (e.g. after the worker was down for
    a while) would otherwise still read as "due `lease_timeout` ago" the
    instant it's claimed -- making `recover_stale_claims` immediately treat
    a freshly-claimed row as an abandoned one and bounce it straight back
    to 'pending' before dispatch ever gets a chance to run.
    """
    now = datetime.now(UTC)
    dialect = session.get_bind().dialect.name

    if dialect == "postgresql":
        due_ids_subquery = (
            select(ScheduledAction.id)
            .where(ScheduledAction.status == "pending", ScheduledAction.due_at <= now)
            .order_by(ScheduledAction.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = await session.execute(
            update(ScheduledAction)
            .where(ScheduledAction.id.in_(due_ids_subquery))
            .values(status="claimed", due_at=now)
            .returning(ScheduledAction.id)
            .execution_options(synchronize_session=False)
        )
        claimed_ids = list(claimed.scalars().all())
        await session.commit()
        if not claimed_ids:
            return []
        result = await session.execute(
            select(ScheduledAction).where(ScheduledAction.id.in_(claimed_ids))
        )
        return list(result.scalars().all())

    result = await session.execute(
        select(ScheduledAction)
        .where(ScheduledAction.status == "pending", ScheduledAction.due_at <= now)
        .order_by(ScheduledAction.due_at)
        .limit(limit)
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = "claimed"
        row.due_at = now
    await session.commit()
    logger.debug("scheduler worker %s claimed %d action(s)", worker_id, len(rows))
    return rows


async def recover_stale_claims(
    session: AsyncSession, lease_timeout: timedelta = DEFAULT_LEASE_TIMEOUT
) -> int:
    """Reclaim a 'claimed' row whose worker crashed mid-dispatch (never
    reached 'done'/'cancelled') so it becomes claimable again -- see this
    module's docstring for why `due_at` (not a locked_at column, which
    doesn't exist here) is the lease clock.
    """
    cutoff = datetime.now(UTC) - lease_timeout
    result = await session.execute(
        update(ScheduledAction)
        .where(
            ScheduledAction.status == "claimed",
            ScheduledAction.processed_at.is_(None),
            ScheduledAction.due_at < cutoff,
        )
        .values(status="pending")
    )
    await session.commit()
    return result.rowcount or 0


async def _dispatch_escalation(action: ScheduledAction, session: AsyncSession) -> None:
    """Escalation dispatch (Phase 15 brief, section 3): if the event is
    still firing and unacknowledged, stage an 'escalation'-trigger outbox
    row for each of the rule's `escalation_channels` and settle 'done'.
    Otherwise (already acknowledged, resolved, or the event/rule/its
    escalation config is gone) raise `_Skip` -- `dispatch()` settles that as
    'cancelled'.

    `trigger='escalation'` never repeats for a given (event, rule): only one
    escalation `ScheduledAction` is ever scheduled per (event, rule) --
    see `app.services.routing._schedule_escalations`'s own dedup check --
    and this dispatches it exactly once, so relying on the outbox's own UQ
    as a defensive backstop (e.g. a lease-recovered redispatch of the same
    action after a crash) is safe here, unlike renotify's (see
    `_dispatch_renotify`) genuinely repeating cycles.
    """
    if action.alert_event_id is None:
        raise _Skip("no event")
    event = await session.get(AlertEvent, action.alert_event_id)
    if event is None or event.status != "firing" or event.acknowledged_at is not None:
        raise _Skip("event resolved, acknowledged, or gone")
    if action.routing_rule_id is None:
        raise _Skip("no rule")

    result = await session.execute(
        select(RoutingRule)
        .where(RoutingRule.id == action.routing_rule_id)
        .options(selectinload(RoutingRule.escalation_channels))
    )
    rule = result.scalar_one_or_none()
    if rule is None or not rule.enabled or not rule.escalation_enabled:
        raise _Skip("rule gone, disabled, or escalation turned off since scheduling")

    # Re-check cross-team consent at dispatch time, not just at save time:
    # a channel's team may have revoked allow_cross_team_escalation between
    # when this rule selected it and now (app/api/channels.py's
    # update_channel already strips the join row when that happens, but
    # this is a defense-in-depth re-check against any row that predates
    # that cleanup, or a direct DB edit).
    allowed_channels: list[Channel] = []
    revoked_ids: list[int] = []
    for c in rule.escalation_channels:
        if c.deleted_at is not None:
            continue
        if c.team_id == rule.team_id or c.allow_cross_team_escalation:
            allowed_channels.append(c)
        else:
            revoked_ids.append(c.id)
    if revoked_ids:
        logger.warning(
            "escalation dispatch: skipping channel(s) %s for rule=%s -- "
            "cross-team escalation no longer allowed",
            revoked_ids,
            rule.id,
        )
    channels = allowed_channels
    if not channels:
        raise _Skip("no (remaining) escalation channels")

    # trigger="firing": an escalation is still a firing-alert notification,
    # just reaching a wider channel set -- the outbox row's OWN trigger
    # ('escalation', see below) is what distinguishes it in delivery
    # history; the embedded AlertNotification's trigger is what templates
    # render against (e.g. "[FIRING] ..." titles), which should read the
    # same as any other firing notification for this event.
    notification = await build_notification_for_event(session, event, trigger="firing")
    payload = notification.model_dump(mode="json")

    staged = 0
    for channel in channels:
        inserted = await stage_outbox_row(
            session,
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            channel=channel,
            team_id=event.team_id,
            trigger="escalation",
            payload=payload,
        )
        if inserted:
            staged += 1
    logger.info(
        "escalation dispatched: event=%s rule=%s channels=%d staged=%d",
        event.id,
        rule.id,
        len(channels),
        staged,
    )


async def schedule_renotify(session: AsyncSession, alert_event_id: int, rule: RoutingRule) -> None:
    """Schedule the next renotify cycle for (event, rule), unless one is
    already pending -- guarantees at most one pending renotify
    `ScheduledAction` per (event, rule) (see this phase's brief, section 4),
    same dedup-via-lookup approach as escalation scheduling
    (`app.services.routing._schedule_escalations`).

    Two call sites: `app/worker/outbox.py`'s `deliver()`, right after a
    successful 'firing' delivery through a rule with
    `renotify_interval_minutes` set (starts the loop), and this module's own
    `_dispatch_renotify` (keeps it going every cycle after that).
    """
    existing = await session.execute(
        select(ScheduledAction.id).where(
            ScheduledAction.alert_event_id == alert_event_id,
            ScheduledAction.routing_rule_id == rule.id,
            ScheduledAction.kind == "renotify",
            ScheduledAction.status == "pending",
        )
    )
    if existing.scalar_one_or_none() is not None:
        return
    session.add(
        ScheduledAction(
            kind="renotify",
            alert_event_id=alert_event_id,
            routing_rule_id=rule.id,
            due_at=datetime.now(UTC) + timedelta(minutes=rule.renotify_interval_minutes),
            status="pending",
        )
    )


async def _dispatch_renotify(action: ScheduledAction, session: AsyncSession) -> None:
    """Renotify dispatch (Phase 15 brief, section 4): if the event is still
    firing and unacknowledged, re-deliver to the rule's own (non-escalation)
    channels and immediately schedule the *next* cycle -- rescheduling
    happens here, in dispatch, not in a separate "on successful delivery"
    hook, so a channel that's currently failing doesn't stall the renotify
    cadence (documented simplicity trade-off in the brief: the next cycle
    is scheduled regardless of whether staging below actually produced a
    new outbox row). Otherwise raise `_Skip` -- settled 'cancelled', and no
    further cycle is scheduled, ending the renotify loop for this
    (event, rule).

    Unlike escalation's single-fire 'escalation' trigger, renotify
    genuinely repeats: reusing a bare `trigger='renotify'` on every cycle
    would collide with the very first cycle's outbox row on
    `NotificationOutbox`'s `(alert_event_id, channel_id, trigger)` unique
    constraint (that constraint has no way to know "this is a *new*
    cycle", not a duplicate of the old one) and silently stop delivering
    after the first ping. Each cycle instead gets its own trigger,
    `f"renotify:{action.id}"` -- unique per `ScheduledAction` row (a fresh
    one is scheduled every cycle, so a fresh id) while still `LIKE
    'renotify:%'`-recognizable in delivery history (see
    `app/api/alerts.py`'s notifications endpoint and the frontend's
    rendering of it).
    """
    if action.alert_event_id is None:
        raise _Skip("no event")
    event = await session.get(AlertEvent, action.alert_event_id)
    if event is None or event.status != "firing" or event.acknowledged_at is not None:
        raise _Skip("event resolved, acknowledged, or gone")
    if action.routing_rule_id is None:
        raise _Skip("no rule")

    result = await session.execute(
        select(RoutingRule)
        .where(RoutingRule.id == action.routing_rule_id)
        .options(selectinload(RoutingRule.channels))
    )
    rule = result.scalar_one_or_none()
    if rule is None or not rule.enabled or not rule.renotify_interval_minutes:
        raise _Skip("rule gone, disabled, or renotify turned off since scheduling")

    channels = [c for c in rule.channels if c.deleted_at is None]
    trigger = f"renotify:{action.id}"
    if channels:
        notification = await build_notification_for_event(session, event, trigger="firing")
        payload = notification.model_dump(mode="json")
        for channel in channels:
            await stage_outbox_row(
                session,
                alert_event_id=event.id,
                routing_rule_id=rule.id,
                channel=channel,
                team_id=event.team_id,
                trigger=trigger,
                payload=payload,
            )

    await schedule_renotify(session, event.id, rule)
    logger.info("renotify dispatched: event=%s rule=%s channels=%d", event.id, rule.id, len(channels))


async def _dispatch_digest_flush(action: ScheduledAction, session: AsyncSession) -> None:
    """Digest flush dispatch (Phase 16 brief, section 3): aggregate every
    row currently parked (`status='digested'`, `digested_into_id` still
    NULL) for this action's channel into a single digest send, or resolve
    the trivial cases without one:

    - 0 parked rows: nothing to do -- settles 'done' (not 'cancelled': this
      isn't "the timer's target went away", it's the ordinary case where a
      brief storm already drained back to normal before the window elapsed).
    - Exactly 1: not worth digesting a single notification for UX reasons
      (see the brief) -- restored to plain 'pending' so the outbox worker
      delivers it individually, same as if it had never been parked.
    - 2+: one new aggregate row (`is_digest=True`, `alert_event_id=None`,
      `trigger='digest'`, `status='pending'`) carrying every parked row's
      own payload, deliverable through the normal outbox claim/deliver path
      (`app/worker/outbox.py`'s `deliver()` special-cases `is_digest` rows
      to call the channel's `send_batch`). Each parked row is linked via
      `digested_into_id` but keeps `status='digested'` -- the aggregate row
      now owns this batch's own delivery/retry history, not each parked row
      individually.

    Deliberately does NOT itself schedule the next flush: if parking keeps
    happening after this flush, the very next parked row's own
    `_ensure_digest_flush_scheduled` call schedules a fresh one (this
    action already settled 'done', so no pending row blocks that) --
    exactly the same §2 dedup-via-lookup logic as the first parking that
    ever scheduled this action, no separate "reschedule" path needed.
    """
    if action.channel_id is None:
        raise _Skip("no channel")
    channel = await session.get(Channel, action.channel_id)
    if channel is None or channel.deleted_at is not None:
        raise _Skip("channel deleted")

    result = await session.execute(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.channel_id == channel.id,
            NotificationOutbox.status == "digested",
            NotificationOutbox.digested_into_id.is_(None),
        )
        .order_by(NotificationOutbox.created_at)
    )
    parked = list(result.scalars().all())

    if not parked:
        logger.info("digest flush: channel=%s nothing parked -- no-op", channel.id)
        return

    if len(parked) == 1:
        parked[0].status = "pending"
        logger.info("digest flush: channel=%s restored 1 parked row to pending", channel.id)
        return

    window_started_at = min(row.created_at for row in parked)
    aggregate = NotificationOutbox(
        alert_event_id=None,
        routing_rule_id=None,
        channel_id=channel.id,
        team_id=channel.team_id,
        trigger="digest",
        payload={
            "notifications": [row.payload for row in parked],
            "count": len(parked),
            "window_started_at": window_started_at.isoformat(),
        },
        is_digest=True,
        status="pending",
    )
    session.add(aggregate)
    await session.flush()

    for row in parked:
        row.digested_into_id = aggregate.id

    logger.info(
        "digest flush: channel=%s aggregated %d parked row(s) into outbox id=%s",
        channel.id,
        len(parked),
        aggregate.id,
    )


DispatchHandler = Callable[[ScheduledAction, AsyncSession], Awaitable[None]]

_HANDLERS: dict[str, DispatchHandler] = {
    "escalation": _dispatch_escalation,
    "renotify": _dispatch_renotify,
    "digest_flush": _dispatch_digest_flush,
}


async def dispatch(action: ScheduledAction, session: AsyncSession) -> None:
    """Dispatch one claimed action, always resolving it to a terminal-ish
    state and committing -- 'done', 'cancelled', or 'pending' (scheduled for
    a retry) -- never leaves it 'claimed' on return. An unknown `kind`
    (a row from a future version's kind this build doesn't recognize, or
    corrupted data) settles 'done' rather than retrying forever -- there is
    no handler that will ever make it succeed, so endless backoff would
    just be a permanently-pending row.
    """
    handler = _HANDLERS.get(action.kind)
    now = datetime.now(UTC)

    if handler is None:
        logger.warning("scheduler: unknown action kind=%r (id=%s) -- marking done", action.kind, action.id)
        action.status = "done"
        action.processed_at = now
        await session.commit()
        return

    try:
        await handler(action, session)
    except _Skip as skip:
        action.status = "cancelled"
        action.processed_at = now
        await session.commit()
        logger.info("scheduled action id=%s kind=%s cancelled: %s", action.id, action.kind, skip)
    except Exception:
        # Any other failure (DB hiccup, a channel/template issue surfaced
        # while staging outbox rows, ...) is a retry candidate, not a
        # permanent cancellation -- restore 'pending' with due_at pushed
        # back rather than leaving the row stuck 'claimed'.
        logger.exception(
            "scheduled action id=%s kind=%s dispatch failed -- retrying in %s",
            action.id,
            action.kind,
            RETRY_BACKOFF,
        )
        action.status = "pending"
        action.due_at = now + RETRY_BACKOFF
        await session.commit()
    else:
        action.status = "done"
        action.processed_at = now
        await session.commit()


async def run_scheduler_tick(
    session_factory: async_sessionmaker[AsyncSession], worker_id: str, *, limit: int = 50
) -> int:
    """Claim one batch, then dispatch each row in its own fresh session --
    same per-row-session reasoning as `app.worker.outbox.run_tick`'s
    docstring (a shared session's rollback on one row's failure would expire
    every other claimed row's ORM state too).
    """
    async with session_factory() as claim_session:
        claimed_ids = [a.id for a in await claim_due_actions(claim_session, worker_id, limit=limit)]

    for action_id in claimed_ids:
        try:
            async with session_factory() as session:
                action = await session.get(ScheduledAction, action_id)
                if action is None:
                    continue
                await dispatch(action, session)
        except Exception:
            logger.exception(
                "scheduler worker: unexpected error dispatching action id=%s -- skipping", action_id
            )
    return len(claimed_ids)


async def maybe_run_retention_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    interval: timedelta = RETENTION_SWEEP_INTERVAL,
) -> dict[str, int] | None:
    """Run `app.services.retention.purge` once every `interval`, gated by
    the 'retention.last_purge_at' `AppSetting` rather than an in-process
    timer -- restart-safe: a restart shortly after the last purge doesn't
    re-run it early just because the process (and any in-memory "last ran
    at") is new. Returns the purge summary, or `None` if it's not due yet.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        last = await get_last_purge_at(session)
    if last is not None and now - last < interval:
        return None

    logger.info("retention: running daily purge sweep")
    summary = await purge(session_factory)
    logger.info("retention: purge summary=%s", summary)
    return summary


# -- report schedules (Phase 20) ------------------------------------------------
#
# Deliberately NOT a ScheduledAction kind (see app.models.report's module
# docstring): a ReportSchedule is a standing, recurring row with no event of
# its own to key a one-shot timer on, so it gets its own claim/dispatch
# lifecycle here instead of trying to force it into the 'pending'->'claimed'
# ->'done'/'cancelled' vocabulary above.
#
# ReportSchedule has no status/locked_by/locked_at columns to build a
# claim+lease on (unlike ScheduledAction/NotificationOutbox) -- next_run_at
# itself doubles as both "when is this due" and "is this currently claimed":
# claim_due_reports provisionally pushes a claimed row's next_run_at
# REPORT_CLAIM_BUMP into the future (long enough that the very next sweep
# tick, 60s later, can't re-claim it; short enough that a crash mid-dispatch
# self-heals within the hour with no separate lease-recovery pass needed).
# dispatch_report_schedule always overwrites that provisional value with the
# real computed next_run_at before returning, whether it succeeds or fails.


async def claim_due_reports(
    session: AsyncSession, worker_id: str, limit: int = 20
) -> list[tuple[int, datetime]]:
    """Atomically claim up to `limit` due, enabled `ReportSchedule` rows.

    Returns `(schedule_id, due_at)` pairs -- `due_at` is each schedule's
    `next_run_at` AT THE MOMENT OF CLAIM, captured before this function
    provisionally advances it. `dispatch_report_schedule` uses `due_at` as
    the report's period-computation reference (see
    `app.services.reports.compute_period`), so how long a row sits claimed
    before dispatch actually runs never changes which period gets reported.

    Dialect-branched exactly like `claim_due_actions`/`app.worker.outbox
    .claim_batch` -- see those functions' docstrings for why (Postgres `FOR
    UPDATE SKIP LOCKED`; SQLite single-process select-then-update). Unlike
    those two, the Postgres branch here keeps the row lock across a plain
    SELECT ... FOR UPDATE and a follow-up UPDATE (rather than one combined
    UPDATE ... RETURNING) specifically so it can read each row's PRE-claim
    next_run_at -- RETURNING only ever reports the POST-update row.
    """
    now = datetime.now(UTC)
    claim_until = now + REPORT_CLAIM_BUMP
    dialect = session.get_bind().dialect.name

    if dialect == "postgresql":
        result = await session.execute(
            select(ReportSchedule.id, ReportSchedule.next_run_at)
            .where(ReportSchedule.enabled.is_(True), ReportSchedule.next_run_at <= now)
            .order_by(ReportSchedule.next_run_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = result.all()
        if not rows:
            return []
        due_map = {schedule_id: due_at for schedule_id, due_at in rows}
        ids = list(due_map.keys())
        await session.execute(
            update(ReportSchedule)
            .where(ReportSchedule.id.in_(ids))
            .values(next_run_at=claim_until)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        return [(schedule_id, due_map[schedule_id]) for schedule_id in ids]

    result = await session.execute(
        select(ReportSchedule)
        .where(ReportSchedule.enabled.is_(True), ReportSchedule.next_run_at <= now)
        .order_by(ReportSchedule.next_run_at)
        .limit(limit)
    )
    rows = list(result.scalars().all())
    pairs = [(row.id, row.next_run_at) for row in rows]
    for row in rows:
        row.next_run_at = claim_until
    await session.commit()
    logger.debug("report sweep worker %s claimed %d schedule(s)", worker_id, len(pairs))
    return pairs


async def dispatch_report_schedule(schedule_id: int, due_at: datetime, session: AsyncSession) -> None:
    """Build + render one claimed report and stage an outbox row per
    channel, then always advance `next_run_at` past whatever the claim's
    provisional bump left it at and record `last_run_at`/`last_status` --
    win or lose, this never leaves a schedule claimed.

    A build/render/staging failure (a stats query error, an unexpected
    exception -- render() itself never raises, see
    `app.services.reports.render_report`) records
    `last_status='error: ...'` and still advances `next_run_at` to the next
    regular occurrence rather than retrying soon: unlike escalation/renotify
    (`RETRY_BACKOFF`), a report is inherently periodic already -- retrying a
    failed weekly report in a minute makes little sense when the next
    scheduled one is only days away, and retrying forever would risk
    duplicate reports once whatever caused the failure clears. An operator
    (or the schedule's own owner) can always trigger `POST
    /reports/{id}/run-now` to retry immediately once the underlying problem
    is fixed.
    """
    result = await session.execute(
        select(ReportSchedule)
        .where(ReportSchedule.id == schedule_id)
        .options(selectinload(ReportSchedule.channels))
    )
    schedule = result.scalar_one_or_none()
    if schedule is None:
        # Deleted between claim and dispatch -- nothing left to do (its
        # provisional next_run_at bump from claim_due_reports is moot, the
        # row is simply gone).
        return

    now = datetime.now(UTC)
    try:
        data = await reports_service.build_report_data(session, schedule, reference=due_at)
        message = await reports_service.render_report(session, schedule, data)

        channels = [c for c in schedule.channels if c.deleted_at is None]
        for channel in channels:
            session.add(
                NotificationOutbox(
                    alert_event_id=None,
                    routing_rule_id=None,
                    channel_id=channel.id,
                    team_id=schedule.team_id,
                    trigger="report",
                    payload={
                        "rendered": message.model_dump(mode="json"),
                        "schedule_id": schedule.id,
                        "period": {
                            "start": data["period_start"].isoformat(),
                            "end": data["period_end"].isoformat(),
                        },
                    },
                    is_digest=False,
                    status="pending",
                )
            )
        schedule.last_status = "ok"
        logger.info(
            "report schedule id=%s dispatched: team=%s channels=%d",
            schedule.id,
            schedule.team_id,
            len(channels),
        )
    except Exception as exc:  # see docstring: never retried immediately.
        logger.exception("report schedule id=%s dispatch failed", schedule.id)
        schedule.last_status = f"error: {type(exc).__name__}: {exc}"[:500]

    schedule.last_run_at = now
    schedule.next_run_at = reports_service.compute_next_run(
        cadence=schedule.cadence,
        weekday=schedule.weekday,
        hour=schedule.hour,
        timezone=schedule.timezone,
        after=now,
    )
    await session.commit()


async def run_report_sweep(
    session_factory: async_sessionmaker[AsyncSession], worker_id: str, *, limit: int = 20
) -> int:
    """Claim one batch of due report schedules, then dispatch each in its
    own fresh session -- same per-row-session reasoning as
    `run_scheduler_tick`/`app.worker.outbox.run_tick`.
    """
    async with session_factory() as claim_session:
        claimed = await claim_due_reports(claim_session, worker_id, limit=limit)

    for schedule_id, due_at in claimed:
        try:
            async with session_factory() as session:
                await dispatch_report_schedule(schedule_id, due_at, session)
        except Exception:
            logger.exception(
                "report sweep worker: unexpected error dispatching schedule id=%s -- skipping",
                schedule_id,
            )
    return len(claimed)
