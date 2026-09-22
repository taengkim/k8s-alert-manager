from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db import get_session
from app.models.user import User
from app.services import audit

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


class UserUpdate(BaseModel):
    is_admin: bool | None = None
    is_active: bool | None = None


def _user_payload(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
    }


@router.get("/users")
async def list_users(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    result = await session.execute(select(User))
    return [_user_payload(u) for u in result.scalars().all()]


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    body: UserUpdate,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    if body.is_admin is not None:
        target.is_admin = body.is_admin
    if body.is_active is not None:
        target.is_active = body.is_active

    await audit.log(
        session,
        user_id=actor.id,
        team_id=None,
        action="user.update",
        object_type="user",
        object_ref=target.username,
        detail=body.model_dump(exclude_unset=True),
    )
    await session.commit()
    await session.refresh(target)
    return _user_payload(target)
