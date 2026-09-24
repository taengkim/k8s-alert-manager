from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Channel(Base):
    """A team's configured notification channel instance.

    `type` is a `NotificationChannel.type_name` (see `app/channels/`) --
    which class of channel this is (email, a plugin's webhook, ...).
    `config_encrypted` is that channel type's `config_schema`, serialized to
    JSON and Fernet-encrypted (see `app/security.py`) before storage, since
    it may hold secrets a third-party plugin's config schema declares
    (SMTP credentials for email live in app settings instead, not here).

    Soft-deleted via `deleted_at`, not a hard `DELETE`: `notification_outbox`
    holds a NOT NULL, non-cascading FK to a channel's id (delivery history
    must survive the channel it was sent through going away), so the
    channel row itself has to keep existing. The team_id+name uniqueness
    constraint only applies among non-deleted rows (see the partial index
    below), so a name is free to reuse once its old channel is deleted.
    """

    __tablename__ = "channels"
    __table_args__ = (
        Index(
            "uq_channel_team_name_active",
            "team_id",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(64))
    config_encrypted: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(default=True)
    # This channel's own default message template (Phase 13), overriding the
    # channel type's built-in default_templates -- overridden in turn by a
    # routing rule's own template_id (see app.services.templating.resolve_template
    # and app/worker/outbox.py's deliver()). ON DELETE SET NULL: deleting the
    # template just reverts this channel to its type's default, rather than
    # blocking (or cascading through) the delete.
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("message_templates.id", ondelete="SET NULL"), nullable=True
    )
    # Phase 15: lets ANOTHER team's routing rule pick this channel as one of
    # its escalation_channels (see app/api/channels.py's
    # GET /channels/escalation-targets and app/api/routes.py's escalation
    # channel validation). False means this channel is only selectable by
    # its own team's rules -- same as before this column existed.
    allow_cross_team_escalation: Mapped[bool] = mapped_column(Boolean, default=False)
    # Phase 16 (storm control): None means unlimited -- a channel receiving
    # more than this many outbox rows (any status, aggregate digest rows
    # excluded -- see app.services.routing._channel_needs_parking) in the
    # trailing hour gets its further notifications parked instead of sent
    # individually when digest_mode='auto'. Unused when digest_mode='off'
    # (no rate limit is ever consulted) or 'always' (every notification is
    # parked regardless of rate).
    rate_limit_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 'off' (default -- no parking, pre-Phase-16 behavior) | 'auto' (park
    # once rate_limit_per_hour is exceeded in the trailing hour) | 'always'
    # (park every notification unconditionally). App-validated, not a DB
    # enum -- same convention as NotificationOutbox.status.
    digest_mode: Mapped[str] = mapped_column(String(16), default="off")
    # How long a parked notification waits before its digest_flush fires
    # (app.worker.scheduler._dispatch_digest_flush) -- see
    # app.services.routing._ensure_digest_flush_scheduled.
    digest_window_minutes: Mapped[int] = mapped_column(Integer, default=5)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ON DELETE SET NULL: deleting the creating user must not delete (or
    # block deleting) the channel itself -- it just loses its "created by"
    # attribution, same pattern as SilenceAudit.created_by.
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
