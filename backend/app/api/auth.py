import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import COOKIE_NAME, get_current_user
from app.config import get_settings
from app.db import get_session
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.security import create_jwt
from app.services import audit
from app.services.auth_sync import sync_user
from app.services.ldap_auth import LdapUnavailableError, authenticate

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str = Field(min_length=1)


async def _user_payload(session: AsyncSession, user: User) -> dict[str, Any]:
    result = await session.execute(
        select(TeamMembership, Team)
        .join(Team, Team.id == TeamMembership.team_id)
        .where(TeamMembership.user_id == user.id)
    )
    teams = [
        {"id": team.id, "slug": team.slug, "name": team.name, "role": membership.role}
        for membership, team in result.all()
    ]
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "is_admin": user.is_admin,
        "teams": teams,
    }


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    # authenticate() does blocking network I/O (ldap3 is synchronous); run it
    # off the event loop so one slow/stuck LDAP call doesn't stall every
    # other request being served by this process.
    try:
        info = await asyncio.to_thread(authenticate, body.username, body.password)
    except LdapUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication backend unavailable",
        ) from exc

    if info is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    user = await sync_user(session, info)

    if not user.is_active:
        await session.rollback()
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user is inactive")

    await audit.log(
        session,
        user_id=user.id,
        team_id=None,
        action="auth.login",
        object_type="user",
        object_ref=user.username,
    )
    await session.commit()
    await session.refresh(user)

    settings = get_settings()
    token = create_jwt(user.id, user.is_admin)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.jwt_ttl_hours * 3600,
    )

    return await _user_payload(session, user)


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    settings = get_settings()
    # Must mirror every flag set_cookie() used above -- a delete_cookie()
    # whose attributes don't match the cookie as originally set (e.g. a
    # differing `secure`/`path`) is a *new* cookie declaration to the
    # browser, not a match against the existing one, so it wouldn't
    # actually clear it.
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )
    return {"status": "ok"}


@router.get("/me")
async def me(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    return await _user_payload(session, user)
