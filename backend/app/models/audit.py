from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_team_created", "team_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # ON DELETE SET NULL: deleting a team writes a 'team.delete' audit row
    # referencing that same team_id in the same transaction, then deletes
    # the team itself -- without this, an FK-enforcing DB (Postgres, or
    # SQLite with PRAGMA foreign_keys=ON) rejects the team delete.
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(64))
    object_type: Mapped[str] = mapped_column(String(64))
    object_ref: Mapped[str] = mapped_column(String(255))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
