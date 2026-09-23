import logging
import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    get_k8s_factory,
    require_admin,
    require_team_role,
)
from app.db import get_session
from app.models.cluster import Cluster
from app.models.team import Team, TeamLdapMapping, TeamMembership
from app.models.user import User
from app.services import audit
from app.services.k8s import (
    K8sBadRequestError,
    K8sClientFactory,
    K8sUnavailableError,
    RuleForbiddenError,
    RuleUpdateConflictError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/teams", tags=["teams"])

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class TeamCreate(BaseModel):
    slug: str
    name: str
    description: str | None = None


class TeamUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class MemberAdd(BaseModel):
    user_id: int
    role: Literal["owner", "member"]


class LdapMappingCreate(BaseModel):
    ldap_group_dn: str = Field(min_length=1)
    role: Literal["owner", "member"]


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


def _team_payload(team: Team) -> dict[str, Any]:
    return {
        "id": team.id,
        "slug": team.slug,
        "name": team.name,
        "description": team.description,
    }


@router.get("")
async def list_teams(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    if user.is_admin:
        result = await session.execute(select(Team))
        teams = result.scalars().all()
    else:
        result = await session.execute(
            select(Team)
            .join(TeamMembership, TeamMembership.team_id == Team.id)
            .where(TeamMembership.user_id == user.id)
        )
        teams = result.scalars().all()
    return [_team_payload(team) for team in teams]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_team(
    body: TeamCreate,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not SLUG_RE.match(body.slug):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid slug")

    existing = await session.execute(select(Team).where(Team.slug == body.slug))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="slug already exists")

    team = Team(slug=body.slug, name=body.name, description=body.description)
    session.add(team)
    await session.flush()

    await audit.log(
        session,
        user_id=user.id,
        team_id=team.id,
        action="team.create",
        object_type="team",
        object_ref=team.slug,
    )
    await session.commit()
    await session.refresh(team)
    return _team_payload(team)


@router.get("/{team_id}")
async def get_team(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)
    return _team_payload(team)


@router.patch("/{team_id}")
async def update_team(
    team_id: int,
    body: TeamUpdate,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)

    if body.name is not None:
        team.name = body.name
    if body.description is not None:
        team.description = body.description

    await audit.log(
        session,
        user_id=user.id,
        team_id=team.id,
        action="team.update",
        object_type="team",
        object_ref=team.slug,
    )
    await session.commit()
    await session.refresh(team)
    return _team_payload(team)


@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: int,
    user: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
) -> None:
    """Deleting a team must not orphan its PrometheusRules: they'd keep
    firing/routing on `kam_team` labels for a team that no longer exists,
    invisible to anyone (the rules list is always team-scoped). So every
    enabled cluster's rules for this team are deleted first.

    Reachability is checked up front (every enabled cluster's rules are
    listed before any delete is attempted): if any cluster can't be
    reached, nothing is deleted at all -- not the rules on other clusters,
    not the team.

    Once cleanup is underway, a per-rule failure that means "this
    particular rule couldn't be deleted for a rule-specific reason"
    (already foreign/unmanaged, a concurrent-modification conflict, a bad
    request) is logged and skipped so it doesn't block the rest. But if the
    *cluster itself* stops responding partway through (K8sUnavailableError)
    or something unexpected happens, the whole operation aborts with 503
    and the team row is left intact -- an admin should never end up with
    the team gone but rules silently left behind because a cluster dropped
    out mid-loop.
    """
    team = await _get_team_or_404(session, team_id)

    clusters = (
        (await session.execute(select(Cluster).where(Cluster.enabled.is_(True))))
        .scalars()
        .all()
    )

    unreachable_detail = (
        "클러스터 {name}에 접근할 수 없어 팀을 삭제할 수 없습니다 (규칙 정리 필요)"
    )

    rules_by_cluster: list[tuple[Cluster, list[dict[str, Any]]]] = []
    for cluster in clusters:
        try:
            raw_rules = await k8s.list_rules(cluster, team.id)
        except (K8sUnavailableError, K8sBadRequestError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=unreachable_detail.format(name=cluster.name),
            ) from exc
        rules_by_cluster.append((cluster, raw_rules))

    for cluster, raw_rules in rules_by_cluster:
        for raw_rule in raw_rules:
            name = (raw_rule.get("metadata") or {}).get("name", "")
            if not name:
                continue
            try:
                await k8s.delete_rule(cluster, name, team.id)
            except (RuleForbiddenError, RuleUpdateConflictError, K8sBadRequestError):
                # Rule-specific: this one couldn't be deleted, but it says
                # nothing about the cluster's health, so don't let it block
                # cleanup of the rest.
                logger.warning(
                    "failed to delete rule '%s' on cluster '%s' during team "
                    "'%s' deletion",
                    name,
                    cluster.name,
                    team.slug,
                )
                continue
            except K8sUnavailableError as exc:
                # The cluster itself dropped out mid-loop. We never call
                # session.commit() until every rule (and the team) is
                # processed, so raising here rolls back every audit row
                # staged so far along with it -- the team row itself is
                # untouched. (Any rules whose k8s-side delete already
                # succeeded in earlier loop iterations stay deleted; only
                # their audit trail is lost. That asymmetry is accepted:
                # k8s and this DB aren't in one transaction.)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=unreachable_detail.format(name=cluster.name),
                ) from exc
            except Exception as exc:
                # Genuinely unexpected -- fail safe the same way as a
                # cluster outage rather than continuing with the team
                # half-cleaned-up.
                logger.exception(
                    "unexpected error deleting rule '%s' on cluster '%s' "
                    "during team '%s' deletion",
                    name,
                    cluster.name,
                    team.slug,
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=unreachable_detail.format(name=cluster.name),
                ) from exc

            await audit.log(
                session,
                user_id=user.id,
                team_id=team.id,
                action="rule.delete",
                object_type="prometheus_rule",
                object_ref=name,
                detail={"cluster_id": cluster.id, "reason": "team_delete"},
            )

    await audit.log(
        session,
        user_id=user.id,
        team_id=team.id,
        action="team.delete",
        object_type="team",
        object_ref=team.slug,
    )

    # SQLite doesn't enforce FKs by default, so clean up dependents
    # explicitly rather than leaving orphaned rows behind.
    await session.execute(
        delete(TeamMembership).where(TeamMembership.team_id == team_id)
    )
    await session.execute(
        delete(TeamLdapMapping).where(TeamLdapMapping.team_id == team_id)
    )
    await session.execute(
        update(Cluster)
        .where(Cluster.heartbeat_team_id == team_id)
        .values(heartbeat_team_id=None)
    )
    await session.delete(team)
    await session.commit()


@router.get("/{team_id}/members")
async def list_members(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(TeamMembership, User)
        .join(User, User.id == TeamMembership.user_id)
        .where(TeamMembership.team_id == team_id)
    )
    return [
        {
            "membership_id": membership.id,
            "user_id": user.id,
            "username": user.username,
            "display_name": user.display_name,
            "role": membership.role,
            "origin": membership.origin,
        }
        for membership, user in result.all()
    ]


@router.post("/{team_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    team_id: int,
    body: MemberAdd,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)

    target_user = await session.get(User, body.user_id)
    if target_user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    existing = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == body.user_id
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already a member")

    membership = TeamMembership(
        team_id=team_id, user_id=body.user_id, role=body.role, origin="manual"
    )
    session.add(membership)
    await session.flush()

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="team.member.add",
        object_type="team_membership",
        object_ref=target_user.username,
        detail={"role": body.role, "origin": "manual"},
    )
    await session.commit()

    return {
        "membership_id": membership.id,
        "user_id": target_user.id,
        "username": target_user.username,
        "display_name": target_user.display_name,
        "role": membership.role,
        "origin": membership.origin,
    }


@router.delete("/{team_id}/members/{membership_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    team_id: int,
    membership_id: int,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Remove a team membership.

    Both manual and ldap-origin rows may be removed by an owner. Removing an
    ldap-origin row is not permanent: if the user's LDAP group membership
    still matches an active TeamLdapMapping, the row is recreated on their
    next login. This is accepted behavior (use ldap-mappings to change
    sync rules for a lasting effect).
    """
    team = await _get_team_or_404(session, team_id)

    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.id == membership_id, TeamMembership.team_id == team_id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="membership not found")

    target_user = await session.get(User, membership.user_id)

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="team.member.remove",
        object_type="team_membership",
        object_ref=target_user.username if target_user else str(membership.user_id),
        detail={"origin": membership.origin},
    )
    await session.delete(membership)
    await session.commit()


@router.get("/{team_id}/ldap-mappings")
async def list_ldap_mappings(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _owner: User = Depends(require_team_role("owner")),
) -> list[dict[str, Any]]:
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(TeamLdapMapping).where(TeamLdapMapping.team_id == team_id)
    )
    return [
        {"id": m.id, "ldap_group_dn": m.ldap_group_dn, "role": m.role}
        for m in result.scalars().all()
    ]


@router.post("/{team_id}/ldap-mappings", status_code=status.HTTP_201_CREATED)
async def create_ldap_mapping(
    team_id: int,
    body: LdapMappingCreate,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)

    mapping = TeamLdapMapping(
        team_id=team_id, ldap_group_dn=body.ldap_group_dn, role=body.role
    )
    session.add(mapping)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="mapping already exists"
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="team.ldap_mapping.add",
        object_type="team_ldap_mapping",
        object_ref=body.ldap_group_dn,
        detail={"role": body.role},
    )
    await session.commit()

    return {"id": mapping.id, "ldap_group_dn": mapping.ldap_group_dn, "role": mapping.role}


@router.delete("/{team_id}/ldap-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ldap_mapping(
    team_id: int,
    mapping_id: int,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> None:
    team = await _get_team_or_404(session, team_id)

    result = await session.execute(
        select(TeamLdapMapping).where(
            TeamLdapMapping.id == mapping_id, TeamLdapMapping.team_id == team_id
        )
    )
    mapping = result.scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mapping not found")

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="team.ldap_mapping.remove",
        object_type="team_ldap_mapping",
        object_ref=mapping.ldap_group_dn,
    )
    await session.delete(mapping)
    await session.commit()
