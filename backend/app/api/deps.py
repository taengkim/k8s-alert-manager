from collections.abc import Callable, Coroutine
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.registry import ChannelRegistry
from app.db import get_session
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.security import decode_jwt
from app.services.cluster_health import ClusterHealthCache
from app.services.k8s import K8sClientFactory

COOKIE_NAME = "kam_token"


def get_k8s_factory(request: Request) -> K8sClientFactory:
    return request.app.state.k8s_factory


def get_cluster_health_cache(request: Request) -> ClusterHealthCache:
    return request.app.state.cluster_health_cache


def get_channel_registry(request: Request) -> ChannelRegistry:
    return request.app.state.channel_registry


async def get_current_user(
    request: Request, session: AsyncSession = Depends(get_session)
) -> User:
    token = request.cookies.get(COOKIE_NAME)
    if token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    try:
        payload = decode_jwt(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        ) from exc

    user_id = int(payload["sub"])
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not authenticated")

    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return user


def require_team_role(
    role: str,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory for team-scoped RBAC.

    Expects the route to declare a `team_id` path parameter. `role='owner'`
    requires an owner membership; `role='member'` accepts owner or member.
    A global admin always passes.
    """

    async def dependency(
        team_id: int,
        user: User = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> User:
        if user.is_admin:
            return user

        result = await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
            )
        )
        membership = result.scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

        if role == "owner" and membership.role != "owner":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

        return user

    return dependency


async def resolve_team_scope(
    team_id: int | None, user: User, session: AsyncSession
) -> Team | None:
    """Authorize the requested team scope and return it (or None for admin's
    unscoped "all teams" view).

    Non-admins must supply a `team_id` they belong to (any role -- owner or
    member). Admins may omit it to see everything, including data with no
    team attribution at all.

    Shared by app.api.alerts (originally the only caller) and
    app.api.stats -- every read view that scopes by team uses this exact
    semantics, so a caller granted access to a team's alerts always sees
    the same team's stats.
    """
    if team_id is None:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="team_id is required",
            )
        return None

    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")

    if not user.is_admin:
        result = await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    return team
