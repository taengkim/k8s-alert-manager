"""Scheduled follow-up actions: escalation and unresolved re-notification
timers.

`app.services.routing.route_event` stages an 'escalation' row when a matched
notify rule has escalation enabled; `app/worker/outbox.py`'s `deliver()`
stages a 'renotify' row after a successful 'firing' delivery when its rule
has a renotify interval set. Both are dispatched later by
`app/worker/scheduler.py`, once `due_at` arrives -- see that module's
docstring for the claim/dispatch/lease lifecycle `status` moves through.
"""

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class ScheduledAction(Base):
    __tablename__ = "scheduled_actions"
    __table_args__ = (
        Index("ix_scheduled_actions_status_due", "status", "due_at"),
        Index("ix_scheduled_actions_event_status", "alert_event_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # 'escalation' | 'renotify' -- app-validated, not a DB enum
    # ON DELETE CASCADE: the event this timer follows up on going away
    # (retention purge, see app.services.retention) leaves nothing for
    # dispatch() to act on, so the timer goes with it.
    alert_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert_events.id", ondelete="CASCADE"), nullable=True
    )
    # ON DELETE SET NULL: deleting the rule that scheduled this action must
    # not delete the action's own row -- same reasoning as
    # NotificationOutbox.routing_rule_id. dispatch() treats a NULL rule (or
    # one with the relevant escalation/renotify field since cleared) as
    # "nothing left to act on" -> cancelled.
    routing_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="SET NULL"), nullable=True
    )
    # Unused by both kinds implemented so far (each acts on a rule's own
    # channel set -- escalation_channels / channels -- not one specific
    # channel); reserved for a future scheduled-action kind that targets a
    # single channel.
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL"), nullable=True
    )
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # 'pending' -> 'claimed' (in-flight dispatch) -> 'done' | 'cancelled', or
    # back to 'pending' with due_at pushed back on a failed dispatch attempt
    # -- see app/worker/scheduler.py's claim_due_actions/dispatch.
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
