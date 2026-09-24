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
        # duplicate notification. Phase 16: a digest AGGREGATE row has
        # alert_event_id=NULL, and SQL's NULL-is-distinct-from-NULL
        # semantics mean this UQ never applies between two such rows (both
        # SQLite and Postgres agree here) -- many aggregate rows for the
        # same channel are expected over time, one per flush. A PARKED row
        # (status='digested', still awaiting a flush) keeps its real
        # alert_event_id, so the UQ still guards it exactly as before.
        UniqueConstraint(
            "alert_event_id", "channel_id", "trigger", name="uq_outbox_event_channel_trigger"
        ),
        Index("ix_outbox_status_next_attempt", "status", "next_attempt_at"),
        # Phase 16: backs both the trailing-hour rate count
        # (app.services.routing._channel_needs_parking) and looking up a
        # channel's still-parked rows at flush time
        # (app.worker.scheduler._dispatch_digest_flush).
        Index("ix_outbox_channel_digest_created", "channel_id", "is_digest", "created_at"),
        Index("ix_outbox_channel_status", "channel_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Phase 16: nullable to allow a digest AGGREGATE row, which summarizes
    # many alert events for one channel rather than belonging to any single
    # one -- see is_digest/digested_into_id below. A normal (non-digest,
    # including a PARKED-but-not-yet-flushed) row always has a real event.
    alert_event_id: Mapped[int | None] = mapped_column(ForeignKey("alert_events.id"), nullable=True)
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
    # never re-derives it from the (possibly since-changed) event row. For a
    # digest aggregate row (is_digest=True), this instead holds
    # {"notifications": [...each parked row's own payload...], "count": N,
    # "window_started_at": ISO8601} -- see
    # app.worker.scheduler._dispatch_digest_flush.
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    # 'pending' -> 'in_progress' -> 'delivered'/'dead' (see
    # app/worker/outbox.py), plus Phase 16's 'digested': a row parked by
    # storm control, awaiting its channel's digest_flush -- never claimed
    # for individual delivery while in this state. App-validated, not a DB
    # enum.
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Phase 16: True only for a digest AGGREGATE row (one summary send
    # standing in for many parked rows) -- never set on the parked rows
    # themselves, which stay is_digest=False throughout (only their status
    # changes to 'digested', then their digested_into_id gets linked once a
    # flush aggregates them). Excluded from the trailing-hour rate count so
    # an aggregate send can never itself trigger more parking.
    is_digest: Mapped[bool] = mapped_column(default=False)
    # Phase 16: set on a parked row once app.worker.scheduler._dispatch_digest_flush
    # aggregates it into a digest send -- points at that aggregate
    # (is_digest=True) row. NULL for every non-parked row, and for a parked
    # row still awaiting its flush. ON DELETE SET NULL: deleting the
    # aggregate row (retention purge) must not delete the parked rows it
    # summarized, which still record real delivery history of their own.
    digested_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("notification_outbox.id", ondelete="SET NULL"), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC)
    )
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
