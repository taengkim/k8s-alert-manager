from datetime import UTC, datetime

from sqlalchemy import ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime


class AlertComment(Base):
    """A free-text comment thread entry on one `alert_events` row.

    CASCADE on `alert_event_id`: a comment has no meaning once its alert
    event is gone (unlike e.g. `NotificationOutbox`, there's no delivery
    history reason to keep it around orphaned).
    """

    __tablename__ = "alert_comments"
    __table_args__ = (
        Index("ix_alert_comments_event_created", "alert_event_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_event_id: Mapped[int] = mapped_column(
        ForeignKey("alert_events.id", ondelete="CASCADE")
    )
    # ON DELETE SET NULL: deleting the author must not delete their
    # comment's content, only its attribution -- same pattern as
    # Channel.created_by.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
