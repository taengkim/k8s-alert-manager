"""The scheduled-action worker: claims and dispatches `ScheduledAction` rows
staged by `app.services.routing.route_event` (kind='escalation') and
`app/worker/outbox.py`'s `deliver()` (kind='renotify'), plus the daily
retention purge sweep (`app.services.retention.purge`).

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.scheduled import ScheduledAction
from app.services.retention import purge
from app.services.routing import build_notification_for_event
from app.services.settings import get_last_purge_at

logger = logging.getLogger(__name__)

RETRY_BACKOFF = timedelta(minutes=1)
DEFAULT_LEASE_TIMEOUT = timedelta(minutes=5)
RETENTION_SWEEP_INTERVAL = timedelta(hours=24)


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
            .values(status="claimed")
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


async def _stage_outbox_row(
    session: AsyncSession,
    event: AlertEvent,
    trigger: str,
    team_id: int,
    channel: Channel,
    rule: RoutingRule,
    notification_payload: dict,
) -> bool:
    """Insert one outbox row, deduped via `NotificationOutbox`'s
    `(alert_event_id, channel_id, trigger)` unique constraint -- the same
    stage-and-catch-IntegrityError pattern as
    `app.services.routing._stage_outbox`, just scoped to a single channel at
    a time (escalation/renotify each already have their own channel list to
    loop over, no need for that function's dict-of-matched-channels shape).
    Returns whether a new row was actually inserted (False on dedup skip).
    """
    outbox = NotificationOutbox(
        alert_event_id=event.id,
        routing_rule_id=rule.id,
        channel_id=channel.id,
        team_id=team_id,
        trigger=trigger,
        payload=dict(notification_payload),
    )
    try:
        async with session.begin_nested():
            session.add(outbox)
            await session.flush()
    except IntegrityError:
        logger.info(
            "scheduled outbox dedup skip: event=%s channel=%s trigger=%s",
            event.id,
            channel.id,
            trigger,
        )
        return False
    return True


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

    channels = [c for c in rule.escalation_channels if c.deleted_at is None]
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
        if await _stage_outbox_row(session, event, "escalation", event.team_id, channel, rule, payload):
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
            await _stage_outbox_row(session, event, trigger, event.team_id, channel, rule, payload)

    await schedule_renotify(session, event.id, rule)
    logger.info("renotify dispatched: event=%s rule=%s channels=%d", event.id, rule.id, len(channels))


DispatchHandler = Callable[[ScheduledAction, AsyncSession], Awaitable[None]]

_HANDLERS: dict[str, DispatchHandler] = {
    "escalation": _dispatch_escalation,
    "renotify": _dispatch_renotify,
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
