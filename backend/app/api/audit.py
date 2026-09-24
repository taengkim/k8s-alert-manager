"""Audit log viewer API: GET /api/v1/audit.

Scoping is deliberately stricter than the general team-scoping helper
(`app.api.deps.resolve_team_scope`): an admin sees everything (optionally
narrowed to one team via `team_id`); a team OWNER sees only their own
team's rows (`team_id` required and validated); a plain member gets 403 --
a team's audit trail is an owner-level concern, unlike its alerts, which
any member may read.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_session
from app.models.audit import AuditLog
from app.models.team import TeamMembership
from app.models.user import User

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


async def _resolve_audit_team_scope(
    team_id: int | None, user: User, session: AsyncSession
) -> int | None:
    """Returns None for an admin's unscoped "every team" view (including a
    `team_id` they explicitly passed, which is just an extra WHERE filter
    for them, not an authorization check), or the caller's own team_id.

    Non-admins: `team_id` is required (422 if omitted, mirroring
    resolve_team_scope's convention) and must name a team where this user
    holds the 'owner' role specifically -- a 'member' membership in that
    same team is not enough, hence this doesn't just delegate to
    resolve_team_scope.
    """
    if user.is_admin:
        return team_id

    if team_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="team_id is required",
        )

    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None or membership.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    return team_id


def _serialize(row: AuditLog, username: str | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "created_at": row.created_at,
        "user_id": row.user_id,
        "username": username,
        "team_id": row.team_id,
        "action": row.action,
        "object_type": row.object_type,
        "object_ref": row.object_ref,
        "detail": row.detail,
    }


@router.get("")
async def list_audit_logs(
    team_id: int | None = Query(default=None),
    user_id: int | None = Query(default=None),
    # Prefix match: "rule." matches every rule.create/update/delete row --
    # see the `.like()` escaping below.
    action: str | None = Query(default=None),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    scoped_team_id = await _resolve_audit_team_scope(team_id, user, session)

    conditions: list[ColumnElement[bool]] = []
    if scoped_team_id is not None:
        conditions.append(AuditLog.team_id == scoped_team_id)
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if action:
        # Escape the caller's literal `%`/`_` so a prefix like "rule." can't
        # accidentally act as a SQL LIKE wildcard -- same pattern as
        # app.api.alerts's alertname search.
        escaped = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(AuditLog.action.like(f"{escaped}%", escape="\\"))
    if from_ts:
        conditions.append(AuditLog.created_at >= from_ts)
    if to_ts:
        conditions.append(AuditLog.created_at <= to_ts)

    total = (
        await session.execute(select(func.count()).select_from(AuditLog).where(*conditions))
    ).scalar_one()

    result = await session.execute(
        select(AuditLog, User.username)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(*conditions)
        # id.desc() breaks ties within the same created_at, same rationale
        # as /alerts/history's pagination ordering.
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = [_serialize(row, username) for row, username in result.all()]

    return {"items": items, "total": total, "page": page, "page_size": page_size}
