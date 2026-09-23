from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime

MAX_COMMENT_LENGTH = 4000


class AlertComment(Base):
    """A free-text comment thread entry on one `alert_events` row.

    CASCADE on `alert_event_id`: a comment has no meaning once its alert
    event is gone (unlike e.g. `NotificationOutbox`, there's no delivery
    history reason to keep it around orphaned).

    The API layer (`CommentCreate` in `app/api/alerts.py`) already rejects a
    body over `MAX_COMMENT_LENGTH` -- this CHECK is defense in depth against
    any writer that bypasses that layer (a script, a future endpoint, a
    direct DB edit), not the primary enforcement point.
    """

    __tablename__ = "alert_comments"
    __table_args__ = (
        Index("ix_alert_comments_event_created", "alert_event_id", "created_at"),
        CheckConstraint(
            f"length(body) <= {MAX_COMMENT_LENGTH}", name="ck_alert_comments_body_length"
        ),
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
