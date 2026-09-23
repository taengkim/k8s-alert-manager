"""Cancellation of pending `ScheduledAction` rows (Phase 15) -- the shared
primitive behind ack's and resolve's "there's nothing left to escalate or
renotify about" hook.

Deliberately its own tiny module (rather than living on
`app.worker.scheduler`, which owns the claim/dispatch side): both
`app.services.routing` (route_event, on a 'resolved' transition) and
`app/api/alerts.py` (ack_alert) need to call this, and `app.worker.scheduler`
itself imports `app.services.routing` (for `build_notification_for_event`)
-- routing importing back from scheduler would be circular. This module has
no dependency on either.
"""

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduled import ScheduledAction


async def cancel_pending(session: AsyncSession, alert_event_id: int) -> int:
    """Cancel every 'pending' `ScheduledAction` for `alert_event_id` --
    called on ack (there's nothing left to escalate) and on a
    firing->resolved transition (there's nothing left to escalate OR
    renotify about). Deliberately does NOT restore a cancelled action on
    unack (see `app/api/alerts.py`'s `unack_alert`) -- simple, and
    documented as such in this phase's brief: an unacked-then-reacked alert
    starts a fresh escalation/renotify cycle from whatever notify rule
    matches it next, rather than resurrecting a timer whose original
    `due_at` may be long past.

    Never commits -- same "stage only, caller owns the transaction"
    contract as everything else `route_event` touches; `ack_alert` commits
    its own request-scoped transaction as usual.
    """
    result = await session.execute(
        update(ScheduledAction)
        .where(ScheduledAction.alert_event_id == alert_event_id, ScheduledAction.status == "pending")
        .values(status="cancelled", processed_at=datetime.now(UTC))
    )
    return result.rowcount or 0
