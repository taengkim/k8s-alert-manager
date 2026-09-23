"""Cross-team alert sharing (Phase 14) CRUD API: an owner team grants a
target team read (`view`) or read+notify (`view_notify`) visibility into
its own alert stream, optionally scoped by matchers -- see
`app/services/sharing.py` for how those are evaluated and
`app/services/routing.py`'s `route_event` for the `view_notify` fan-out.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_team_role
from app.api.routes import MatcherInput, _validate_matchers
from app.db import get_session
from app.models.share import AlertShare
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.services import audit

team_router = APIRouter(prefix="/api/v1/teams/{team_id}/shares", tags=["shares"])
router = APIRouter(prefix="/api/v1/shares", tags=["shares"])
shared_with_me_router = APIRouter(
    prefix="/api/v1/teams/{team_id}/shared-with-me", tags=["shares"]
)


class ShareCreate(BaseModel):
    target_team_id: int
    mode: Literal["view", "view_notify"]
    matchers: list[MatcherInput] | None = None


class ShareUpdate(BaseModel):
    """All fields optional: only keys actually present in the request body
    are applied (see `update_share`'s use of `model_fields_set`) -- this is
    a partial update via PUT, not a full-replace like `RouteWrite`. A
    `matchers` key present with `null`/`[]` explicitly clears the share back
    to "every alert this team owns", same as omitting it entirely on create.
    """

    mode: Literal["view", "view_notify"] | None = None
    matchers: list[MatcherInput] | None = None


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


async def _get_share_or_404(session: AsyncSession, share_id: int) -> AlertShare:
    share = await session.get(AlertShare, share_id)
    if share is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="share not found")
    return share


async def _require_owner_team_role(session: AsyncSession, team_id: int, user: User) -> None:
    """Same RBAC rule as `deps.require_team_role('owner')`, callable after
    the fact once a share's `owner_team_id` is known -- `/shares/{id}` is
    keyed by share id, not team_id, so the path-param-driven dependency
    factory doesn't apply (mirrors `app.api.routes._require_team_role`).
    """
    if user.is_admin:
        return
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None or membership.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _matchers_to_json(matchers: list[MatcherInput] | None) -> list[dict[str, Any]] | None:
    return [m.model_dump(exclude_none=True) for m in matchers] if matchers else None


def _serialize_outgoing(share: AlertShare, target_team: Team) -> dict[str, Any]:
    return {
        "id": share.id,
        "owner_team_id": share.owner_team_id,
        "target_team_id": share.target_team_id,
        "target_team_slug": target_team.slug,
        "target_team_name": target_team.name,
        "mode": share.mode,
        "matchers": share.matchers,
        "created_at": share.created_at,
    }


def _serialize_incoming(share: AlertShare, owner_team: Team) -> dict[str, Any]:
    return {
        "id": share.id,
        "owner_team_id": share.owner_team_id,
        "owner_team_slug": owner_team.slug,
        "owner_team_name": owner_team.name,
        "mode": share.mode,
        "matchers": share.matchers,
        "created_at": share.created_at,
    }


@team_router.get("")
async def list_outgoing_shares(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    """Every share this team is the *owner* of -- what it has shared out,
    and to whom.
    """
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(AlertShare, Team)
        .join(Team, Team.id == AlertShare.target_team_id)
        .where(AlertShare.owner_team_id == team_id)
    )
    return [_serialize_outgoing(share, target) for share, target in result.all()]


@team_router.post("", status_code=status.HTTP_201_CREATED)
async def create_share(
    team_id: int,
    body: ShareCreate,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_team_or_404(session, team_id)
    if body.target_team_id == team_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="target_team_id must differ from the owner team",
        )
    target = await session.get(Team, body.target_team_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown target_team_id: {body.target_team_id}",
        )
    if body.matchers:
        _validate_matchers(body.matchers)

    share = AlertShare(
        owner_team_id=team_id,
        target_team_id=body.target_team_id,
        mode=body.mode,
        matchers=_matchers_to_json(body.matchers),
        created_by=actor.id,
    )
    session.add(share)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a share to this target team already exists",
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team_id,
        action="share.create",
        object_type="alert_share",
        object_ref=str(share.id),
        detail={"target_team_id": body.target_team_id, "mode": body.mode},
    )
    await session.commit()
    await session.refresh(share)
    return _serialize_outgoing(share, target)


@router.put("/{share_id}")
async def update_share(
    share_id: int,
    body: ShareUpdate,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    share = await _get_share_or_404(session, share_id)
    await _require_owner_team_role(session, share.owner_team_id, actor)

    fields_set = body.model_fields_set
    if "mode" in fields_set and body.mode is not None:
        share.mode = body.mode
    if "matchers" in fields_set:
        if body.matchers:
            _validate_matchers(body.matchers)
        share.matchers = _matchers_to_json(body.matchers)

    await session.flush()
    await audit.log(
        session,
        user_id=actor.id,
        team_id=share.owner_team_id,
        action="share.update",
        object_type="alert_share",
        object_ref=str(share.id),
        detail={"mode": share.mode},
    )
    await session.commit()
    await session.refresh(share)
    target = await session.get(Team, share.target_team_id)
    assert target is not None
    return _serialize_outgoing(share, target)


@router.delete("/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_share(
    share_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    share = await _get_share_or_404(session, share_id)
    await _require_owner_team_role(session, share.owner_team_id, actor)

    await audit.log(
        session,
        user_id=actor.id,
        team_id=share.owner_team_id,
        action="share.delete",
        object_type="alert_share",
        object_ref=str(share.id),
    )
    await session.delete(share)
    await session.commit()


@shared_with_me_router.get("")
async def list_incoming_shares(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    """Every share this team is the *target* of -- what other teams have
    shared with it, read-only (there's nothing to manage on the receiving
    side; editing/revoking is always the owner's call).
    """
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(AlertShare, Team)
        .join(Team, Team.id == AlertShare.owner_team_id)
        .where(AlertShare.target_team_id == team_id)
    )
    return [_serialize_incoming(share, owner) for share, owner in result.all()]
