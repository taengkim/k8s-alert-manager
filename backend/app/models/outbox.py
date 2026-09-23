"""Outbox queue: one row per (alert_event, channel, trigger) notification
still to be delivered.

`app/services/routing.py` inserts rows here, inside the ingest transaction
-- staging only. Only `app/worker/outbox.py` ever dispatches them; see that
module for the claim/deliver/backoff lifecycle `status` moves through.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base, UTCDateTime


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        # The dedup backstop routing.py relies on: route_event may evaluate
        # the same (event, channel, trigger) more than once across repeated
        # webhook deliveries -- this UQ is what turns a second insert
        # attempt into a no-op (IntegrityError -> skip) instead of a
        # duplicate notification.
        UniqueConstraint(
            "alert_event_id", "channel_id", "trigger", name="uq_outbox_event_channel_trigger"
        ),
        Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_event_id: Mapped[int] = mapped_column(ForeignKey("alert_events.id"))
    # ON DELETE SET NULL: deleting the rule that caused this outbox row must
    # not delete delivery history -- same reasoning as AlertEvent.team_id.
    routing_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("routing_rules.id", ondelete="SET NULL"), nullable=True
    )
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    # 'firing' | 'resolved' | 'escalation' (Phase 15, single-fire per
    # (event, rule)) | f'renotify:{scheduled_action_id}' (Phase 15 -- each
    # renotify cycle gets its own value, since a bare 'renotify' reused
    # every cycle would collide with the first cycle's row on this table's
    # own (alert_event_id, channel_id, trigger) unique constraint below --
    # see app/worker/scheduler.py's _dispatch_renotify docstring).
    trigger: Mapped[str] = mapped_column(String(32))
    # A frozen AlertNotification, serialized at routing time -- delivery
    # never re-derives it from the (possibly since-changed) event row.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC)
    )
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
