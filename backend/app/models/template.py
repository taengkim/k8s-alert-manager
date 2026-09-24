"""Message templates: a team's own Jinja2 source for how a notification's
title/body/body_html get rendered before delivery (see
`app/services/templating.py` for the sandboxed rendering engine, and
`app/worker/outbox.py`'s `deliver()` for where a template is resolved and
rendered -- at delivery time, not routing time).

`kind` is a free string, not a DB CHECK constraint: 'alert' (this module's
own `ALERT_TEMPLATE_KIND`) and 'report' (Phase 20, see
`app.services.reports.REPORT_TEMPLATE_KIND`) are both supported today,
enforced at the API layer (see `app/api/templates.py`'s `SUPPORTED_KINDS`)
-- leaving the column unconstrained is what let Phase 20's 'report' kind
land without a schema migration.

The two kinds are not interchangeable: only an 'alert'-kind template may be
assigned to a `Channel`/`RoutingRule` (see `app/api/channels.py`'s
`_validate_template_ownership` and `app/api/routes.py`'s
`_validate_template`), and only a 'report'-kind template may be assigned to
a `ReportSchedule` (see `app/api/reports.py`'s own `_validate_template`) --
a template's own render context is entirely different between the two (an
`AlertNotification` vs. `app.services.reports.ReportData`), so cross-
assigning would render blank/nonsense output via the sandboxed environment's
lenient `ChainableUndefined` rather than failing loudly.
"""

from datetime import UTC, datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, UTCDateTime

MAX_TEMPLATE_LENGTH = 16384

# This module's own kind -- see module docstring. 'report' (Phase 20) is
# `app.services.reports.REPORT_TEMPLATE_KIND`, not duplicated here to avoid
# a needless cross-import; app/api/templates.py's SUPPORTED_KINDS combines
# both.
ALERT_TEMPLATE_KIND = "alert"


class MessageTemplate(Base):
    __tablename__ = "message_templates"
    __table_args__ = (
        UniqueConstraint("team_id", "name", name="uq_message_template_team_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 'alert' | 'report' (Phase 20) -- see module docstring.
    kind: Mapped[str] = mapped_column(String(32), default=ALERT_TEMPLATE_KIND)
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
