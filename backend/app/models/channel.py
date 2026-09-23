from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
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
    """

    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("team_id", "name", name="uq_channel_team_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[str] = mapped_column(String(64))
    config_encrypted: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(default=True)
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
