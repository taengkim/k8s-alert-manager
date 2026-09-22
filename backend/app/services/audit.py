"""Audit log writer. Inserts only — the caller owns the transaction/commit."""

from typing import Any

from app.models.audit import AuditLog


async def log(
    session,
    *,
    user_id: int | None,
    team_id: int | None,
    action: str,
    object_type: str,
    object_ref: str,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLog(
            user_id=user_id,
            team_id=team_id,
            action=action,
            object_type=object_type,
            object_ref=object_ref,
            detail=detail,
        )
    )
