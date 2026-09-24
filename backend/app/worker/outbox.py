"""The outbox delivery worker: claims `NotificationOutbox` rows staged by
`app.services.routing.route_event` and dispatches them through the channel
they're addressed to.

Deliberately FastAPI-free (no `fastapi` import anywhere in this module, nor
transitively via its imports) -- this is meant to be runnable as a
standalone process (see `app/worker/runner.py`) as well as embedded in the
API process's lifespan (see `app/main.py`).
"""

import asyncio
import json
import logging
import random
import time
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.base import AlertNotification, RenderedMessage
from app.channels.registry import ChannelRegistry
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.security import decrypt_str
from app.services.events_hub import Hub
from app.services.templating import APP_DEFAULT_TEMPLATES, render, resolve_template
from app.worker.heartbeat import (
    SWEEP_INTERVAL_SECONDS as HEARTBEAT_SWEEP_INTERVAL_SECONDS,
)
from app.worker.heartbeat import sweep as run_heartbeat_sweep
from app.worker.scheduler import (
    maybe_run_retention_sweep,
    recover_stale_claims,
    run_report_sweep,
    run_scheduler_tick,
    schedule_renotify,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 1800
JITTER_MAX_SECONDS = 15
DELIVERY_TIMEOUT_SECONDS = 30
DEFAULT_LEASE_TIMEOUT = timedelta(minutes=5)


async def claim_batch(
    session: AsyncSession, worker_id: str, limit: int = 20
) -> list[NotificationOutbox]:
    """Atomically claim up to `limit` due 'pending' rows for this worker,
    marking them 'in_progress'.

    Dialect-branched: Postgres uses `FOR UPDATE SKIP LOCKED` so multiple
    worker processes can claim disjoint batches concurrently without
    blocking on each other. SQLite has no such row-locking, so it falls
    back to plain SELECT-then-UPDATE under the (documented) assumption that
    only a single worker process runs against a SQLite database at a time.
    """
    now = datetime.now(UTC)
    dialect = session.get_bind().dialect.name

    if dialect == "postgresql":
        due_ids_subquery = (
            select(NotificationOutbox.id)
            .where(
                NotificationOutbox.status == "pending",
                NotificationOutbox.next_attempt_at <= now,
            )
            .order_by(NotificationOutbox.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = await session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id.in_(due_ids_subquery))
            .values(status="in_progress", locked_by=worker_id, locked_at=now)
            .returning(NotificationOutbox.id)
            # Nothing is loaded in this Session that this UPDATE could
            # invalidate, so there's nothing to synchronize -- and leaving
            # the default ("evaluate") composes ORM auto-synchronization
            # with a RETURNING fetch in a way that isn't the well-trodden
            # path on Postgres. No live-Postgres test for this branch in
            # this repo yet (Phase 21's packaging work brings a Postgres
            # profile); reasoned correct against SQLAlchemy 2.0's docs.
            .execution_options(synchronize_session=False)
        )
        claimed_ids = list(claimed.scalars().all())
        await session.commit()
        if not claimed_ids:
            return []
        result = await session.execute(
            select(NotificationOutbox).where(NotificationOutbox.id.in_(claimed_ids))
        )
        return list(result.scalars().all())

    # SQLite: single-process assumption -- no concurrent worker can race
    # this claim, so a plain select-then-update is safe.
    result = await session.execute(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.status == "pending",
            NotificationOutbox.next_attempt_at <= now,
        )
        .order_by(NotificationOutbox.next_attempt_at)
        .limit(limit)
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = "in_progress"
        row.locked_by = worker_id
        row.locked_at = now
    await session.commit()
    return rows


def _truncate_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


async def _mark_dead(session: AsyncSession, row: NotificationOutbox, reason: str) -> None:
    row.status = "dead"
    row.last_error = reason[:500]
    row.locked_by = None
    row.locked_at = None
    await session.commit()


async def _mark_delivery_failure(
    session: AsyncSession, row: NotificationOutbox, exc: Exception
) -> None:
    row.attempts += 1
    row.last_error = _truncate_error(exc)
    row.locked_by = None
    row.locked_at = None
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "dead"
    else:
        row.status = "pending"
        backoff = min(BASE_BACKOFF_SECONDS * 2**row.attempts, MAX_BACKOFF_SECONDS)
        jitter = random.uniform(0, JITTER_MAX_SECONDS)
        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff + jitter)
    await session.commit()


async def deliver(row: NotificationOutbox, registry: ChannelRegistry, session: AsyncSession) -> None:
    """Deliver one claimed row. Always resolves the row to a terminal-ish
    state and commits -- 'delivered', 'pending' (scheduled for retry), or
    'dead' -- never leaves it 'in_progress' on return.

    Phase 16: a digest aggregate row (`row.is_digest`) goes through
    `send_batch()` instead of `send()` -- see the branch below. Everything
    else about this row's lifecycle (claim, retry/backoff, dead-lettering,
    lease recovery) is identical to a normal row; only which channel method
    gets called, and what gets rendered for it, differs.
    """
    channel = await session.get(Channel, row.channel_id)
    if channel is None:
        await _mark_dead(session, row, "channel disabled or deleted")
        return
    if channel.deleted_at is not None:
        await _mark_dead(session, row, "channel deleted")
        return
    if not channel.enabled:
        await _mark_dead(session, row, "channel disabled or deleted")
        return

    channel_cls = registry.get(channel.type)
    if channel_cls is None:
        await _mark_dead(session, row, f"unknown channel type '{channel.type}'")
        return

    try:
        raw_config = json.loads(decrypt_str(channel.config_encrypted))
        config = channel_cls.config_schema(**raw_config)
    except (json.JSONDecodeError, ValidationError) as exc:
        # The stored config's JSON/schema itself is broken (corrupted JSON,
        # or a schema that changed shape underneath an old config) --
        # retrying can't fix bytes that never change between attempts, so
        # this goes straight to dead instead of burning through 8 backoff
        # attempts (~40 minutes) for something no amount of waiting will
        # resolve.
        #
        # `cryptography.fernet.InvalidToken` (decrypt failure) is
        # deliberately NOT included here, even though it also means
        # "this config will never successfully decrypt as-is": it usually
        # means the *key* is wrong (KAM_SECRET_KEY rotated or
        # misconfigured on this process), which is an operator-fixable
        # misconfiguration rather than corrupted data. Fast-deading it
        # would take down the entire pending queue in a single tick the
        # moment a key issue hits; falling through to the `except
        # Exception` below (the normal retry-then-dead-after-8-attempts
        # path) instead gives an operator ~40 minutes to fix the key
        # before anything is permanently lost.
        await _mark_dead(session, row, f"invalid channel config: {type(exc).__name__}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await _mark_delivery_failure(session, row, exc)
        return

    fallback_note: str | None = None
    try:
        instance = channel_cls(config)

        # Template resolution + rendering happens here, at delivery time,
        # not back when route_event staged this row -- row.payload is a
        # frozen AlertNotification snapshot, but which template applies
        # (and that template's own source) is read fresh on every attempt,
        # so an edit to a team's template takes effect for anything still
        # queued, not just alerts routed after the edit.
        rule = (
            await session.get(RoutingRule, row.routing_rule_id)
            if row.routing_rule_id is not None
            else None
        )

        if row.trigger == "report":
            # Phase 20: a scheduled-report row has no alert_event_id,
            # routing_rule_id, or template resolution of its own -- rendering
            # already happened once, at schedule-sweep time
            # (app.worker.scheduler.dispatch_report_schedule), and the
            # result is frozen into row.payload["rendered"]. There's no
            # per-attempt "current template" concept to re-resolve the way
            # there is for an alert: a render failure at generation time
            # already fell back to the default report template there (see
            # app.services.reports.render_report), so there's nothing left
            # to fall back to on a delivery retry either.
            message = RenderedMessage(**row.payload["rendered"])
            async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
                await instance.send_message(message)
        elif row.is_digest:
            # Phase 16: a digest aggregate row has no routing_rule_id of its
            # own (rule is None, per row.routing_rule_id being NULL -- see
            # app.worker.scheduler._dispatch_digest_flush) and no single
            # AlertNotification -- row.payload["notifications"] is a list of
            # each parked row's own frozen payload instead. Every item
            # shares one template resolution (channel's own template, or
            # its type's/the app's default -- there's no per-rule override
            # to consult since there's no rule), but each still gets its
            # own render pass (a template can reference per-item fields
            # like alertname/severity).
            notifications = [
                AlertNotification(**item) for item in row.payload["notifications"]
            ]
            template_strs = await resolve_template(session, None, channel.template_id)
            if template_strs is None:
                template_strs = channel_cls.default_templates or APP_DEFAULT_TEMPLATES
            msgs: list[RenderedMessage] = []
            fell_back = 0
            for notification in notifications:
                outcome = await render(template_strs, notification)
                msgs.append(outcome.message)
                if outcome.fallback_used:
                    fell_back += 1
            if fell_back:
                fallback_note = (
                    f"template render failed for {fell_back}/{len(notifications)} "
                    "digest item(s); fallback used"
                )
            async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
                await instance.send_batch(notifications, msgs)
        else:
            notification = AlertNotification(**row.payload)
            template_strs = await resolve_template(
                session,
                rule.template_id if rule is not None else None,
                channel.template_id,
            )
            if template_strs is None:
                template_strs = channel_cls.default_templates or APP_DEFAULT_TEMPLATES
            outcome = await render(template_strs, notification)
            if outcome.fallback_used:
                # A broken custom template must not block the alert --
                # render() already fell back to the default template and
                # this still counts as delivered, but the fallback is
                # recorded so a team notices their template is broken
                # instead of silently getting the wrong message forever.
                fallback_note = f"template render failed: {outcome.error}; fallback used"

            async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
                await instance.send(notification, outcome.message)
    except Exception as exc:  # noqa: BLE001
        # Deliberately catch-all (ChannelDeliveryError, the asyncio.timeout
        # block's TimeoutError, and anything else a channel's send()/
        # send_batch() could raise): every transient failure mode gets the
        # same backoff-and-retry treatment, never an unhandled exception
        # that would kill the worker loop. render() itself never raises
        # (see its docstring) -- this only catches failures from send()/
        # send_batch() or the channel's own construction.
        await _mark_delivery_failure(session, row, exc)
        return

    row.status = "delivered"
    row.delivered_at = datetime.now(UTC)
    row.locked_by = None
    row.locked_at = None
    if fallback_note:
        row.last_error = fallback_note[:500]

    # Phase 15: a successful 'firing' delivery through a rule with
    # renotify_interval_minutes set starts (or keeps alive) the unresolved
    # re-notification loop -- see app/worker/scheduler.py's schedule_renotify
    # and _dispatch_renotify. 'resolved' deliveries never schedule one
    # (there's nothing left to renotify about), and neither does a rule
    # with no renotify interval configured.
    if row.trigger == "firing" and rule is not None and rule.renotify_interval_minutes:
        await schedule_renotify(session, row.alert_event_id, rule)

    await session.commit()


async def recover_stale_leases(
    session: AsyncSession, lease_timeout: timedelta = DEFAULT_LEASE_TIMEOUT
) -> int:
    """Reclaim rows a worker claimed but never resolved (crashed, killed
    mid-delivery, ...) so they become claimable again instead of stuck
    'in_progress' forever.
    """
    cutoff = datetime.now(UTC) - lease_timeout
    result = await session.execute(
        update(NotificationOutbox)
        .where(NotificationOutbox.status == "in_progress", NotificationOutbox.locked_at < cutoff)
        .values(status="pending", locked_by=None, locked_at=None)
    )
    await session.commit()
    return result.rowcount or 0


async def run_tick(
    session_factory: async_sessionmaker[AsyncSession],
    registry: ChannelRegistry,
    worker_id: str,
    *,
    limit: int = 20,
) -> int:
    """Claim one batch, then deliver each row in its own session.

    Each row gets a fresh session (re-fetched by id) rather than sharing
    one across the whole batch: `Session.rollback()` -- needed to recover
    from a row whose delivery broke outside `deliver()`'s own try/except --
    expires every object still attached to that session, so a shared
    session would leave the *other*, perfectly fine claimed rows in this
    batch expired too. Their next attribute access would then attempt an
    implicit lazy-refresh outside of any awaited call, which raises
    `MissingGreenlet` under SQLAlchemy's asyncio extension -- silently
    skipping the rest of the batch instead of actually delivering it. A
    per-row session sidesteps this entirely: one row's failure can't touch
    any other row's ORM state.
    """
    async with session_factory() as claim_session:
        claimed_ids = [row.id for row in await claim_batch(claim_session, worker_id, limit=limit)]

    for row_id in claimed_ids:
        try:
            async with session_factory() as session:
                row = await session.get(NotificationOutbox, row_id)
                if row is None:
                    continue
                await deliver(row, registry, session)
        except Exception:
            # deliver() already catches every failure mode a channel's
            # send() can raise internally -- reaching here means
            # something broke outside that (e.g. a malformed stored
            # payload, or the DB connection dropping mid-commit). This
            # row's session is simply discarded on the way out of the
            # `async with` block (closing it implicitly rolls back), and
            # the row stays 'in_progress' for the next lease-recovery pass
            # to reclaim -- it never touches any other row's session.
            logger.exception(
                "outbox worker: unexpected error delivering row id=%s -- skipping",
                row_id,
            )
    return len(claimed_ids)


async def run_loop(
    stop_event: asyncio.Event,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    registry: ChannelRegistry,
    worker_id: str,
    poll_interval: float = 3.0,
    lease_recovery_interval: float = 60.0,
    lease_timeout: timedelta = DEFAULT_LEASE_TIMEOUT,
    scheduler_interval: float = 30.0,
    heartbeat_sweep_interval: float = HEARTBEAT_SWEEP_INTERVAL_SECONDS,
    report_sweep_interval: float = 60.0,
    hub: Hub | None = None,
) -> None:
    """Poll for due outbox rows until `stop_event` is set.

    Phase 15: also ticks `app.worker.scheduler` on its own, coarser
    `scheduler_interval` (default 30s -- escalation/renotify timers don't
    need 3s-poll-loop precision) from inside this same loop/task, rather
    than running a second independent loop -- the simpler of the two
    options this phase's brief considered, since a second loop would need
    its own task, its own stop_event handling, and its own lifespan wiring
    for no real benefit here. The daily retention purge sweep
    (`maybe_run_retention_sweep`) piggybacks on that same 30s tick too --
    it no-ops immediately unless 24h have actually passed (gated by the
    'retention.last_purge_at' AppSetting), so checking every 30s costs one
    cheap indexed read, not a purge attempt every 30s.

    Phase 17: `app.worker.heartbeat.sweep` ticks on its own fixed
    `heartbeat_sweep_interval` (default 60s, per that phase's brief -- unlike
    retention it isn't gated by an AppSetting, since 60s against
    minutes-scale timeouts is cheap to just always run). Same "piggyback on
    the one loop that already exists" reasoning as the scheduler/retention
    ticks above -- no separate task or stop_event handling of its own.

    Phase 20: `app.worker.scheduler.run_report_sweep` ticks on its own fixed
    `report_sweep_interval` (default 60s, per that phase's brief) -- same
    "piggyback on the one loop that already exists, fixed interval, not
    AppSetting-gated" reasoning as the heartbeat sweep above (a due report
    schedule is comparatively rare -- checking every 60s is cheap).

    Robust by design: any exception during a tick (claim, deliver, lease
    recovery, scheduler dispatch, retention, heartbeat sweep, or report
    sweep) is logged and swallowed so one bad iteration never kills the loop
    -- the next poll just tries again.

    Phase 18: `hub` is optional (default `None`) and passed straight through
    to `app.worker.heartbeat.sweep` -- this module stays FastAPI-free and
    importable by the standalone worker process either way (see
    `app/worker/runner.py`, which intentionally never supplies one: a
    standalone worker process has no SSE subscribers of its own to publish
    to, so a heartbeat-lost alert it injects reaches connected clients only
    via their next query refetch, not the live feed). The embedded worker
    (this same loop, run as a background task from `app/main.py`'s lifespan)
    is the only caller that passes the app's real `Hub`.
    """
    last_lease_recovery = time.monotonic() - lease_recovery_interval  # run once immediately
    last_scheduler_tick = time.monotonic() - scheduler_interval  # run once immediately
    last_heartbeat_sweep = time.monotonic() - heartbeat_sweep_interval  # run once immediately
    last_report_sweep = time.monotonic() - report_sweep_interval  # run once immediately
    while not stop_event.is_set():
        try:
            if time.monotonic() - last_lease_recovery >= lease_recovery_interval:
                async with session_factory() as session:
                    recovered = await recover_stale_leases(session, lease_timeout)
                    if recovered:
                        logger.warning("outbox worker: recovered %d stale lease(s)", recovered)
                last_lease_recovery = time.monotonic()

            await run_tick(session_factory, registry, worker_id)

            if time.monotonic() - last_scheduler_tick >= scheduler_interval:
                async with session_factory() as session:
                    recovered = await recover_stale_claims(session)
                    if recovered:
                        logger.warning("scheduler worker: recovered %d stale claim(s)", recovered)
                await run_scheduler_tick(session_factory, worker_id)
                await maybe_run_retention_sweep(session_factory)
                last_scheduler_tick = time.monotonic()

            if time.monotonic() - last_heartbeat_sweep >= heartbeat_sweep_interval:
                summary = await run_heartbeat_sweep(session_factory, hub=hub)
                if summary["went_missing"]:
                    logger.warning(
                        "heartbeat sweep: cluster(s) went missing: %s", summary["went_missing"]
                    )
                last_heartbeat_sweep = time.monotonic()

            if time.monotonic() - last_report_sweep >= report_sweep_interval:
                dispatched = await run_report_sweep(session_factory, worker_id)
                if dispatched:
                    logger.info("report sweep: dispatched %d schedule(s)", dispatched)
                last_report_sweep = time.monotonic()
        except Exception:
            logger.exception("outbox worker: tick failed -- continuing")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except TimeoutError:
            pass
