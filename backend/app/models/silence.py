from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base


class SilenceAudit(Base):
    """Team-attribution + history record for a silence created through the
    app. Alertmanager itself is the source of truth for the silence's
    matchers/timing/state; this table only exists so the app can say *who*
    (which team) created it, since AM has no such concept.

    A silence created directly against Alertmanager (outside the app) has
    no matching row here -- surfaced to the API/UI as "external".
    """

    __tablename__ = "silence_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    am_silence_id: Mapped[str] = mapped_column(String(64), index=True)
    cluster_id: Mapped[int] = mapped_column(ForeignKey("clusters.id"))
    # ON DELETE SET NULL: deleting a team must not lose this silence's
    # history -- it just becomes unattributed ("external") going forward,
    # the same as one that was never attributed to a team at all.
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    matchers: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(String(1024))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
