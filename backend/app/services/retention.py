"""Time-based data retention: purges old terminal-state rows so the
database doesn't grow unbounded forever. Runs from
`app/worker/scheduler.py`'s daily sweep (`maybe_run_retention_sweep`) and
on demand via `POST /api/v1/admin/retention/purge`
(`app/api/admin_settings.py`).

Every window below is read from `AppSetting` at purge time (see
`app.services.settings`), falling back to its `SETTING_DEFAULTS` entry when
unset -- an admin can tune retention at runtime (`GET`/`PUT
/api/v1/admin/settings`), and a brand-new deployment with no `AppSetting`
rows yet still purges sensibly out of the box.

Deliberately FastAPI-free: this runs from the scheduler's background loop,
not just from a request.
"""

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Delete, Select, and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.alert import AlertEvent
from app.models.audit import AuditLog
from app.models.outbox import NotificationOutbox
from app.models.scheduled import ScheduledAction
from app.services import audit
from app.services.settings import get_int_setting, set_last_purge_at

logger = logging.getLogger(__name__)

BATCH_SIZE = 1000

# The admin settings API's whitelist (app/api/admin_settings.py): only these
# keys are readable/writable there, and these are the only defaults purge()
# ever falls back to.
SETTING_DEFAULTS: dict[str, int] = {
    "retention.alert_events_days": 90,
    "retention.test_alert_events_days": 7,
    "retention.notification_outbox_days": 30,
    "retention.audit_log_days": 365,
    "retention.scheduled_actions_days": 7,
}


@dataclass
class PurgeSummary:
    alert_events: int = 0
    notification_outbox: int = 0
    scheduled_actions: int = 0
    audit_logs: int = 0


async def load_settings(session: AsyncSession) -> dict[str, int]:
    """Every retention window, resolved against its stored `AppSetting`
    (falling back to `SETTING_DEFAULTS`) -- shared by `purge()` and the
    admin settings `GET` endpoint, so both report the exact same effective
    values.
    """
    return {
        key: await get_int_setting(session, key, default)
        for key, default in SETTING_DEFAULTS.items()
    }


async def _delete_batches(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    select_ids: Select[tuple[int]],
    delete_by_ids: Callable[[list[int]], Delete],
) -> int:
    """Repeatedly select up to `BATCH_SIZE` eligible ids and delete them,
    each batch its own committed transaction -- a crash mid-purge loses at
    most one batch's worth of progress, not the whole sweep. Shared by
    every purge target below except alert_events (see `_purge_alert_events`,
    which needs a second DELETE per batch for its dependent outbox rows).
    """
    total = 0
    while True:
        async with session_factory() as session:
            result = await session.execute(select_ids.limit(BATCH_SIZE))
            ids = [row[0] for row in result.all()]
            if not ids:
                return total
            await session.execute(delete_by_ids(ids))
            await session.commit()
        total += len(ids)
        if len(ids) < BATCH_SIZE:
            return total


async def _purge_alert_events(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    resolved_cutoff: datetime,
    test_cutoff: datetime,
) -> int:
    """Purge `alert_events` past their retention window -- two cohorts,
    either of which qualifies a row for deletion:

    - non-test, `status='resolved'`, `last_received_at < resolved_cutoff`.
      A firing (non-test) event is NEVER purged, regardless of age -- there
      is no cutoff parameter that could make one eligible, by construction.
    - `is_test` (any status, firing included), `last_received_at <
      test_cutoff` -- test alerts are meant to be short-lived, so this
      ignores the firing/resolved distinction the non-test cohort respects
      (a forgotten `resolve-test` call must not pin a test row forever).

    Each batch deletes that batch's own `notification_outbox` rows FIRST,
    regardless of THEIR own 30-day window (`NotificationOutbox.alert_event_id`
    has no `ON DELETE` clause -- deleting the event first would violate that
    FK, and once the event itself is gone there is nothing left for
    `GET /alerts/history/{id}/notifications` to show anyway).
    `alert_comments` cascades at the database level (`ON DELETE CASCADE`),
    and so does any lingering `scheduled_actions` row for the event.
    """
    select_ids = select(AlertEvent.id).where(
        or_(
            and_(
                AlertEvent.is_test.is_(False),
                AlertEvent.status == "resolved",
                AlertEvent.last_received_at < resolved_cutoff,
            ),
            and_(AlertEvent.is_test.is_(True), AlertEvent.last_received_at < test_cutoff),
        )
    )

    total = 0
    while True:
        async with session_factory() as session:
            result = await session.execute(select_ids.limit(BATCH_SIZE))
            ids = [row[0] for row in result.all()]
            if not ids:
                return total
            await session.execute(
                delete(NotificationOutbox).where(NotificationOutbox.alert_event_id.in_(ids))
            )
            await session.execute(delete(AlertEvent).where(AlertEvent.id.in_(ids)))
            await session.commit()
        total += len(ids)
        if len(ids) < BATCH_SIZE:
            return total


async def _purge_notification_outbox(
    session_factory: async_sessionmaker[AsyncSession], *, cutoff: datetime
) -> int:
    """Purge `notification_outbox` rows past their retention window --
    'delivered'/'dead' rows older than `cutoff`, same base query as every
    other `_delete_batches` target, EXCEPT: when a row in that batch is a
    digest AGGREGATE row (`is_digest=True`), this also force-deletes every
    row it aggregated (`digested_into_id` pointing at it), regardless of
    THEIR own age or status (Phase 16).

    Without this, a parked ('digested') row would outlive its aggregate --
    `digested_into_id`'s `ON DELETE SET NULL` would fire, leaving a row
    whose real delivery/retry history was entirely superseded by that now-
    gone aggregate (see `app.worker.scheduler._dispatch_digest_flush`)
    looking exactly like one still awaiting a flush: a dead end in the
    notification history UI, potentially forever (a parked row tied to a
    firing event that never resolves is never swept via its own event
    either -- `_purge_alert_events` never purges a firing event). Mirrors
    `_purge_alert_events`'s own "force-delete dependents regardless of
    their own window" pattern below, just in the other direction (a parked
    row whose OWNING EVENT is purged first is unaffected by this function --
    it already goes with that event, aggregate-linked or not).

    Returns the true count of `notification_outbox` rows removed (the
    batch's own qualifying rows PLUS every child force-deleted alongside
    an aggregate among them) -- unlike `_purge_alert_events`'s `alert_events`
    return value, which deliberately does NOT count the outbox rows it
    force-deletes alongside each event, this field exists specifically to
    summarize `notification_outbox` deletions, so undercounting here would
    just be a wrong number in the purge summary/audit log for no reason.
    """
    select_ids = select(NotificationOutbox.id).where(
        NotificationOutbox.status.in_(("delivered", "dead")), NotificationOutbox.created_at < cutoff
    )
    total = 0
    while True:
        async with session_factory() as session:
            result = await session.execute(select_ids.limit(BATCH_SIZE))
            ids = [row[0] for row in result.all()]
            if not ids:
                return total
            children_result = await session.execute(
                delete(NotificationOutbox).where(NotificationOutbox.digested_into_id.in_(ids))
            )
            await session.execute(delete(NotificationOutbox).where(NotificationOutbox.id.in_(ids)))
            await session.commit()
        total += len(ids) + (children_result.rowcount or 0)
        if len(ids) < BATCH_SIZE:
            return total


async def purge(
    session_factory: async_sessionmaker[AsyncSession], *, actor_user_id: int | None = None
) -> dict[str, int]:
    """Run every retention target's purge and record the outcome.

    `actor_user_id` is `None` for the scheduler's own daily sweep (a
    system action, not a user one -- `AuditLog.user_id` is nullable exactly
    for this) and the acting admin's id when triggered via
    `POST /api/v1/admin/retention/purge`.

    Order: scheduled_actions and notification_outbox first (both
    independent of alert_events' own purge), then alert_events (which
    forces its own outbox rows out regardless of their age -- see
    `_purge_alert_events`), then audit_log. Nothing here depends on that
    order for correctness; it's simply "smallest/most independent tables
    first".
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        settings = await load_settings(session)

    summary = PurgeSummary()

    summary.scheduled_actions = await _delete_batches(
        session_factory,
        select_ids=select(ScheduledAction.id).where(
            ScheduledAction.status.in_(("done", "cancelled")),
            ScheduledAction.processed_at.is_not(None),
            ScheduledAction.processed_at
            < now - timedelta(days=settings["retention.scheduled_actions_days"]),
        ),
        delete_by_ids=lambda ids: delete(ScheduledAction).where(ScheduledAction.id.in_(ids)),
    )

    summary.notification_outbox = await _purge_notification_outbox(
        session_factory,
        cutoff=now - timedelta(days=settings["retention.notification_outbox_days"]),
    )

    summary.alert_events = await _purge_alert_events(
        session_factory,
        resolved_cutoff=now - timedelta(days=settings["retention.alert_events_days"]),
        test_cutoff=now - timedelta(days=settings["retention.test_alert_events_days"]),
    )

    summary.audit_logs = await _delete_batches(
        session_factory,
        select_ids=select(AuditLog.id).where(
            AuditLog.created_at < now - timedelta(days=settings["retention.audit_log_days"])
        ),
        delete_by_ids=lambda ids: delete(AuditLog).where(AuditLog.id.in_(ids)),
    )

    result = asdict(summary)
    logger.info("retention purge summary=%s", result)

    async with session_factory() as session:
        await audit.log(
            session,
            user_id=actor_user_id,
            team_id=None,
            action="retention.purge",
            object_type="retention",
            object_ref="purge",
            detail=result,
        )
        await set_last_purge_at(session, now)
        await session.commit()

    return result
