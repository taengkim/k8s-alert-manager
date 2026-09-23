"""Routing rule CRUD + preview API.

Delivery itself (which outbox rows actually get sent) is
`app/worker/outbox.py`'s job; this only manages rule *definitions* and lets
a team try a draft rule against its own recent alert history before saving
it (`POST .../routes/preview`).
"""

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, require_team_role
from app.db import get_session
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.routing import RoutingMatcher, RoutingRule
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.services import audit
from app.services.routing import (
    build_transient_matchers,
    build_transient_rule,
    preview_rule,
)

MAX_PATTERN_LENGTH = 512
VALID_SEVERITIES = {"critical", "warning", "info", "none"}

team_router = APIRouter(prefix="/api/v1/teams/{team_id}/routes", tags=["routes"])
router = APIRouter(prefix="/api/v1/routes", tags=["routes"])

_RULE_OPTIONS = (selectinload(RoutingRule.matchers), selectinload(RoutingRule.channels))


class MatcherInput(BaseModel):
    kind: Literal["include", "exclude"]
    target: Literal["alertname", "label", "annotation"]
    key: str | None = None
    pattern: str


class RouteWrite(BaseModel):
    name: str
    description: str | None = None
    action: Literal["notify", "suppress"]
    enabled: bool = True
    notify_on_firing: bool = True
    notify_on_resolved: bool = False
    severities: list[str] | None = None
    namespaces_include: list[str] | None = None
    namespaces_exclude: list[str] | None = None
    clusters: list[int] | None = None
    channel_ids: list[int] = []
    matchers: list[MatcherInput] = []


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


async def _get_rule_or_404(session: AsyncSession, rule_id: int) -> RoutingRule:
    result = await session.execute(
        select(RoutingRule).where(RoutingRule.id == rule_id).options(*_RULE_OPTIONS)
    )
    rule = result.scalar_one_or_none()
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="route not found")
    return rule


async def _require_team_role(session: AsyncSession, team_id: int, user: User, role: str) -> None:
    """Same RBAC rule as `deps.require_team_role`, callable after the fact
    once a rule's team_id is known -- `/routes/{id}` is keyed by rule id,
    not team_id, so the path-param-driven dependency factory doesn't apply.
    """
    if user.is_admin:
        return
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


def _validate_pattern(pattern: str, *, field: str) -> None:
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field}: pattern exceeds {MAX_PATTERN_LENGTH} characters",
        )
    try:
        re.compile(pattern)
    except re.error as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{field}: not a valid regular expression ({exc})",
        ) from exc


def _validate_matchers(matchers: list[MatcherInput]) -> None:
    for position, matcher in enumerate(matchers):
        _validate_pattern(matcher.pattern, field=f"matchers[{position}].pattern")
        if matcher.target in ("label", "annotation") and not matcher.key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"matchers[{position}].key is required for target='{matcher.target}'",
            )


def _validate_namespace_patterns(patterns: list[str] | None, *, field: str) -> None:
    for position, pattern in enumerate(patterns or []):
        _validate_pattern(pattern, field=f"{field}[{position}]")


def _validate_severities(severities: list[str] | None) -> None:
    if not severities:
        return
    invalid = sorted({s for s in severities if s.lower() not in VALID_SEVERITIES})
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid severities: {invalid} (allowed: {sorted(VALID_SEVERITIES)})",
        )


async def _resolve_channels(
    session: AsyncSession, team_id: int, action: str, channel_ids: list[int]
) -> list[Channel]:
    if action == "suppress":
        if channel_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="a suppress rule must not have any channels",
            )
        return []

    if not channel_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="a notify rule requires at least one channel",
        )

    unique_ids = list(dict.fromkeys(channel_ids))
    # A soft-deleted channel is excluded here, same as any other unknown
    # id -- it's gone from every picker, so referencing it is a validation
    # error, not a legitimate (if unusual) request.
    result = await session.execute(
        select(Channel).where(Channel.id.in_(unique_ids), Channel.deleted_at.is_(None))
    )
    by_id = {c.id: c for c in result.scalars().all()}

    missing = [cid for cid in unique_ids if cid not in by_id]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown channel_ids: {missing}",
        )
    wrong_team = [cid for cid in unique_ids if by_id[cid].team_id != team_id]
    if wrong_team:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"channel_ids not owned by this team: {wrong_team}",
        )
    return [by_id[cid] for cid in unique_ids]


async def _validate_clusters_exist(session: AsyncSession, cluster_ids: list[int] | None) -> None:
    if not cluster_ids:
        return
    result = await session.execute(
        select(Cluster.id).where(Cluster.id.in_(set(cluster_ids)))
    )
    found = set(result.scalars().all())
    missing = sorted(set(cluster_ids) - found)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"unknown cluster ids: {missing}",
        )


def _validate_body(body: RouteWrite) -> None:
    _validate_severities(body.severities)
    _validate_namespace_patterns(body.namespaces_include, field="namespaces_include")
    _validate_namespace_patterns(body.namespaces_exclude, field="namespaces_exclude")
    _validate_matchers(body.matchers)


def _serialize(rule: RoutingRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "team_id": rule.team_id,
        "name": rule.name,
        "description": rule.description,
        "action": rule.action,
        "enabled": rule.enabled,
        "notify_on_firing": rule.notify_on_firing,
        "notify_on_resolved": rule.notify_on_resolved,
        "severities": rule.severities,
        "namespaces_include": rule.namespaces_include,
        "namespaces_exclude": rule.namespaces_exclude,
        "clusters": rule.clusters,
        "channel_ids": [c.id for c in rule.channels],
        "matchers": [
            {
                "kind": m.kind,
                "target": m.target,
                "key": m.key,
                "pattern": m.pattern,
                "position": m.position,
            }
            for m in sorted(rule.matchers, key=lambda m: m.position)
        ],
        "created_at": rule.created_at,
        "updated_at": rule.updated_at,
    }


@team_router.get("")
async def list_routes(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(RoutingRule).where(RoutingRule.team_id == team_id).options(*_RULE_OPTIONS)
    )
    return [_serialize(rule) for rule in result.scalars().all()]


@team_router.post("", status_code=status.HTTP_201_CREATED)
async def create_route(
    team_id: int,
    body: RouteWrite,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_team_or_404(session, team_id)
    _validate_body(body)
    await _validate_clusters_exist(session, body.clusters)
    channels = await _resolve_channels(session, team_id, body.action, body.channel_ids)

    rule = RoutingRule(
        team_id=team_id,
        name=body.name,
        description=body.description,
        action=body.action,
        enabled=body.enabled,
        notify_on_firing=body.notify_on_firing,
        notify_on_resolved=body.notify_on_resolved,
        severities=body.severities,
        namespaces_include=body.namespaces_include,
        namespaces_exclude=body.namespaces_exclude,
        clusters=body.clusters,
        channels=channels,
        matchers=[
            RoutingMatcher(
                kind=m.kind, target=m.target, key=m.key, pattern=m.pattern, position=position
            )
            for position, m in enumerate(body.matchers)
        ],
    )
    session.add(rule)
    await session.flush()

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team_id,
        action="route.create",
        object_type="routing_rule",
        object_ref=rule.name,
        detail={"action": rule.action},
    )
    await session.commit()
    await session.refresh(rule, attribute_names=["matchers", "channels"])
    return _serialize(rule)


@team_router.post("/preview")
async def preview_routes(
    team_id: int,
    body: RouteWrite,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    await _get_team_or_404(session, team_id)
    _validate_body(body)

    draft_rule = build_transient_rule(
        team_id=team_id,
        name=body.name,
        description=body.description,
        action=body.action,
        enabled=body.enabled,
        notify_on_firing=body.notify_on_firing,
        notify_on_resolved=body.notify_on_resolved,
        severities=body.severities,
        namespaces_include=body.namespaces_include,
        namespaces_exclude=body.namespaces_exclude,
        clusters=body.clusters,
    )
    draft_matchers = build_transient_matchers([m.model_dump() for m in body.matchers])

    results = await preview_rule(session, team_id, draft_rule, draft_matchers)
    return [
        {
            "event_id": r.event_id,
            "alertname": r.alertname,
            "severity": r.severity,
            "namespace": r.namespace,
            "cluster": r.cluster,
            "status": r.status,
            "verdict": r.verdict.value,
            "blocking_matcher_position": r.blocking_matcher_position,
        }
        for r in results
    ]


@router.get("/{route_id}")
async def get_route(
    route_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rule = await _get_rule_or_404(session, route_id)
    await _require_team_role(session, rule.team_id, actor, "member")
    return _serialize(rule)


@router.put("/{route_id}")
async def update_route(
    route_id: int,
    body: RouteWrite,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    rule = await _get_rule_or_404(session, route_id)
    await _require_team_role(session, rule.team_id, actor, "owner")

    _validate_body(body)
    await _validate_clusters_exist(session, body.clusters)
    channels = await _resolve_channels(session, rule.team_id, body.action, body.channel_ids)

    rule.name = body.name
    rule.description = body.description
    rule.action = body.action
    rule.enabled = body.enabled
    rule.notify_on_firing = body.notify_on_firing
    rule.notify_on_resolved = body.notify_on_resolved
    rule.severities = body.severities
    rule.namespaces_include = body.namespaces_include
    rule.namespaces_exclude = body.namespaces_exclude
    rule.clusters = body.clusters
    # Full replace, per the API contract -- both are already eagerly loaded
    # (via _get_rule_or_404's selectinload), so this diffs cleanly against
    # the in-memory collections rather than triggering a lazy load.
    rule.channels = channels
    rule.matchers = [
        RoutingMatcher(kind=m.kind, target=m.target, key=m.key, pattern=m.pattern, position=position)
        for position, m in enumerate(body.matchers)
    ]

    await session.flush()

    await audit.log(
        session,
        user_id=actor.id,
        team_id=rule.team_id,
        action="route.update",
        object_type="routing_rule",
        object_ref=rule.name,
        detail={"action": rule.action},
    )
    await session.commit()
    await session.refresh(rule, attribute_names=["matchers", "channels"])
    return _serialize(rule)


@router.delete("/{route_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_route(
    route_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    rule = await _get_rule_or_404(session, route_id)
    await _require_team_role(session, rule.team_id, actor, "owner")

    await audit.log(
        session,
        user_id=actor.id,
        team_id=rule.team_id,
        action="route.delete",
        object_type="routing_rule",
        object_ref=rule.name,
    )
    await session.delete(rule)
    await session.commit()
