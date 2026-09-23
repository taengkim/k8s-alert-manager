"""Message templates: a team's own Jinja2 source for how a notification's
title/body/body_html get rendered before delivery (see
`app/services/templating.py` for the sandboxed rendering engine, and
`app/worker/outbox.py`'s `deliver()` for where a template is resolved and
rendered -- at delivery time, not routing time).

`kind` is a free string, not a DB CHECK constraint: only 'alert' is
supported today (enforced at the API layer, see `app/api/templates.py`),
but leaving the column unconstrained means Phase 20's 'report' kind can
land without a schema migration.
"""

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime

MAX_TEMPLATE_LENGTH = 16384


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_message_template_team_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 'alert' only for now -- see module docstring.
    kind: Mapped[str] = mapped_column(String(32), default="alert")
    title_template: Mapped[str] = mapped_column(Text)
    body_template: Mapped[str] = mapped_column(Text)
    body_html_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ON DELETE SET NULL: deleting the creating user must not delete (or
    # block deleting) the template -- same pattern as Channel.created_by.
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
