"""Alertmanager silence management: create/list/expire silences on a
cluster's Alertmanager, with team-attribution history kept in
`silence_audit` (Alertmanager itself has no notion of teams).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_session
from app.models.cluster import Cluster
from app.models.silence import SilenceAudit
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.services import audit
from app.services.alertmanager import (
    AlertmanagerBadRequestError,
    AlertmanagerClient,
    AlertmanagerUnavailableError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/silences", tags=["silences"])


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


class MatcherIn(BaseModel):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)
    is_regex: bool = False


class SilenceCreate(BaseModel):
    cluster_id: int
    team_id: int
    matchers: list[MatcherIn] = Field(min_length=1)
    duration_minutes: int | None = None
    ends_at: datetime | None = None
    comment: str = Field(min_length=1)

    @model_validator(mode="after")
    def _exactly_one_duration(self) -> "SilenceCreate":
        if (self.duration_minutes is None) == (self.ends_at is None):
            raise ValueError("exactly one of duration_minutes or ends_at is required")
        if self.duration_minutes is not None and self.duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive")
        return self


async def _get_cluster_or_404(session: AsyncSession, cluster_id: int) -> Cluster:
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")
    return cluster


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


async def _is_team_member(session: AsyncSession, team_id: int, user: User) -> bool:
    if user.is_admin:
        return True
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
        )
    )
    return result.scalar_one_or_none() is not None


def _compute_state(starts_at: datetime, ends_at: datetime) -> str:
    now = datetime.now(UTC)
    if ends_at <= now:
        return "expired"
    if starts_at > now:
        return "pending"
    return "active"


def _serialize(
    raw: dict[str, Any], team: dict[str, Any] | None, cluster: dict[str, Any]
) -> dict[str, Any]:
    status_obj = raw.get("status") or {}
    return {
        "id": raw.get("id"),
        "matchers": raw.get("matchers") or [],
        "startsAt": raw.get("startsAt"),
        "endsAt": raw.get("endsAt"),
        "createdBy": raw.get("createdBy"),
        "comment": raw.get("comment"),
        "status": status_obj.get("state"),
        "team": team,
        "cluster": cluster,
    }


async def _fetch_cluster_silences(
    cluster: Cluster, http_client: httpx.AsyncClient
) -> tuple[Cluster, list[dict[str, Any]]]:
    raw_silences = await AlertmanagerClient(cluster, http_client).get_silences()
    return cluster, raw_silences


@router.get("")
async def list_silences(
    cluster_id: list[int] = Query(default=[]),
    team_id: int | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    """Fan out across one or more clusters' Alertmanagers -- `cluster_id`
    repeats as a query param (`?cluster_id=1&cluster_id=2`); a single value
    behaves exactly as it did before this endpoint supported multiple, and
    an id that doesn't resolve to a real cluster still 404s the whole
    request (matches the old single-cluster behavior exactly). Omitting
    `cluster_id` entirely defaults to every *enabled* cluster, mirroring
    `GET /alerts/live`'s fan-out default.

    An explicit id is still intersected with `enabled` (silently excluded,
    not 404'd, if the cluster exists but is disabled) -- otherwise the same
    header ClusterFilter selection would scope this view differently from
    `/alerts/live`'s, which always intersects enabled.

    Unlike `/alerts/live`, one cluster's Alertmanager being unreachable here
    still 503s the whole request rather than degrading to a per-cluster
    `errors[]` entry: silences are a lower-traffic, more deliberate view (an
    operator explicitly checking/managing suppressions) where a silently
    incomplete list is worse than a clear failure -- and it's what every
    existing single-cluster caller already depends on.
    """
    if cluster_id:
        resolved = [await _get_cluster_or_404(session, cid) for cid in cluster_id]
        clusters = [c for c in resolved if c.enabled]
    else:
        clusters = (
            (await session.execute(select(Cluster).where(Cluster.enabled.is_(True))))
            .scalars()
            .all()
        )

    if team_id is not None:
        await _get_team_or_404(session, team_id)
        if not await _is_team_member(session, team_id, user):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    try:
        fetch_results = await asyncio.gather(
            *(_fetch_cluster_silences(cluster, http_client) for cluster in clusters)
        )
    except AlertmanagerUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    cluster_ids = [c.id for c in clusters]
    am_ids = [raw.get("id") for _, raws in fetch_results for raw in raws if raw.get("id")]
    # Keyed by (cluster_id, am_silence_id), not am_silence_id alone: two
    # different clusters' Alertmanagers mint their own silence UUIDs
    # independently, so a bare am_silence_id isn't unique once more than one
    # cluster is in play.
    audit_by_id: dict[tuple[int, str], SilenceAudit] = {}
    if am_ids:
        result = await session.execute(
            select(SilenceAudit).where(
                SilenceAudit.cluster_id.in_(cluster_ids),
                SilenceAudit.am_silence_id.in_(am_ids),
            )
        )
        for row in result.scalars().all():
            audit_by_id[(row.cluster_id, row.am_silence_id)] = row

    team_ids_needed = {row.team_id for row in audit_by_id.values() if row.team_id is not None}
    teams_by_id: dict[int, Team] = {}
    if team_ids_needed:
        result = await session.execute(select(Team).where(Team.id.in_(team_ids_needed)))
        teams_by_id = {t.id: t for t in result.scalars().all()}

    # For the unscoped ("all my stuff") view, a member sees their own teams'
    # silences plus unattributed ("external") ones -- AM itself isn't a
    # security boundary here (see docs/design.md "주요 리스크"), so this is
    # about relevance/noise, not access control. team_id filter above is
    # what actually enforces membership.
    member_team_ids: set[int] | None = None
    if team_id is None and not user.is_admin:
        result = await session.execute(
            select(TeamMembership.team_id).where(TeamMembership.user_id == user.id)
        )
        member_team_ids = set(result.scalars().all())

    items = []
    for cluster, raw_silences in fetch_results:
        for raw in raw_silences:
            row = audit_by_id.get((cluster.id, raw.get("id")))
            row_team_id = row.team_id if row else None

            if team_id is not None:
                if row_team_id != team_id:
                    continue
            elif (
                member_team_ids is not None
                and row_team_id is not None
                and row_team_id not in member_team_ids
            ):
                continue

            team_out = None
            if row_team_id is not None:
                team = teams_by_id.get(row_team_id)
                if team is not None:
                    team_out = {"id": team.id, "slug": team.slug}

            items.append(
                _serialize(raw, team_out, {"id": cluster.id, "name": cluster.name})
            )

    return {"silences": items}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_silence(
    body: SilenceCreate,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, body.cluster_id)
    team = await _get_team_or_404(session, body.team_id)
    if not await _is_team_member(session, team.id, actor):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    starts_at = datetime.now(UTC)
    if body.ends_at is not None:
        ends_at = body.ends_at if body.ends_at.tzinfo is not None else body.ends_at.replace(tzinfo=UTC)
    else:
        ends_at = starts_at + timedelta(minutes=body.duration_minutes)

    matchers = [
        {"name": m.name, "value": m.value, "isRegex": m.is_regex, "isEqual": True}
        for m in body.matchers
    ]
    payload = {
        "matchers": matchers,
        "startsAt": starts_at.isoformat(),
        "endsAt": ends_at.isoformat(),
        "createdBy": actor.username,
        "comment": body.comment,
    }

    client = AlertmanagerClient(cluster, http_client)
    try:
        silence_id = await client.create_silence(payload)
    except AlertmanagerBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except AlertmanagerUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    try:
        session.add(
            SilenceAudit(
                am_silence_id=silence_id,
                cluster_id=cluster.id,
                team_id=team.id,
                created_by=actor.id,
                matchers=matchers,
                starts_at=starts_at,
                ends_at=ends_at,
                comment=body.comment,
            )
        )
        await audit.log(
            session,
            user_id=actor.id,
            team_id=team.id,
            action="silence.create",
            object_type="silence",
            object_ref=silence_id,
            detail={"cluster_id": cluster.id},
        )
        await session.commit()
    except Exception:
        logger.exception(
            "failed to persist silence_audit for AM silence '%s' on cluster '%s'; "
            "attempting compensating expire",
            silence_id,
            cluster.name,
        )
        # Compensate before rolling back: rollback expires every ORM object
        # touched in this transaction, including `cluster` -- and refreshing
        # an expired attribute (e.g. cluster.alertmanager_url, read inside
        # expire_silence) requires a sync-style lazy load that can't run
        # here, raising MissingGreenlet instead of the error we're already
        # busy handling.
        try:
            await client.expire_silence(silence_id)
        except AlertmanagerUnavailableError:
            logger.exception(
                "compensating expire also failed for AM silence '%s' on cluster '%s' -- "
                "it now exists in Alertmanager with no audit record",
                silence_id,
                cluster.name,
            )
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to record silence",
        ) from None

    return {
        "id": silence_id,
        "matchers": matchers,
        "startsAt": payload["startsAt"],
        "endsAt": payload["endsAt"],
        "createdBy": actor.username,
        "comment": body.comment,
        "status": _compute_state(starts_at, ends_at),
        "team": {"id": team.id, "slug": team.slug},
    }


@router.delete("/{am_silence_id}", status_code=status.HTTP_204_NO_CONTENT)
async def expire_silence(
    am_silence_id: str,
    cluster_id: int = Query(...),
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> None:
    cluster = await _get_cluster_or_404(session, cluster_id)

    result = await session.execute(
        select(SilenceAudit).where(
            SilenceAudit.cluster_id == cluster.id,
            SilenceAudit.am_silence_id == am_silence_id,
        )
    )
    row = result.scalar_one_or_none()
    team_id = row.team_id if row else None

    # A silence with a team on its audit row is that team's (member or
    # admin) to expire. One with no team -- either never attributed, or its
    # team has since been deleted -- is "external" and admin-only, since no
    # membership check can meaningfully apply.
    if team_id is not None:
        if not await _is_team_member(session, team_id, actor):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    elif not actor.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    try:
        await AlertmanagerClient(cluster, http_client).expire_silence(am_silence_id)
    except AlertmanagerUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team_id,
        action="silence.expire",
        object_type="silence",
        object_ref=am_silence_id,
        detail={"cluster_id": cluster.id},
    )
    await session.commit()
