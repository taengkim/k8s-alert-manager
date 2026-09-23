"""PrometheusRule CRUD API: team-scoped alert rule management (raw PromQL
mode -- a guided threshold builder is Phase 5), plus JSON export/import
(Phase 12 -- see `app.services.rule_transfer`).
"""

import json
from typing import Annotated, Any

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_k8s_factory, require_team_role
from app.db import get_session
from app.models.cluster import Cluster
from app.models.team import Team
from app.models.user import User
from app.services import audit
from app.services.k8s import (
    MANAGED_BY_LABEL,
    MANAGED_BY_VALUE,
    TEAM_ID_LABEL,
    K8sBadRequestError,
    K8sClientFactory,
    K8sUnavailableError,
    RuleConflictError,
    RuleForbiddenError,
    RuleUpdateConflictError,
)
from app.services.prometheus import PrometheusClient, PrometheusUnavailableError
from app.services.rule_transfer import (
    ConflictStrategy,
    UnsupportedExportVersion,
    build_export_envelope,
    execute_import,
    parse_envelope,
    plan_import,
)
from app.services.rules import (
    RULE_SLUG_RE,
    RuleWrite,
    build_prometheus_rule,
    parse_prometheus_rule,
    rule_object_name,
)

router = APIRouter(prefix="/api/v1/teams/{team_id}/rules", tags=["rules"])
validate_router = APIRouter(prefix="/api/v1/rules", tags=["rules"])

# Annotated (not a shared `Path(...)` default value) so each parameter gets
# its own FieldInfo copy rather than three route functions sharing one
# mutable instance.
SlugParam = Annotated[str, Path(pattern=RULE_SLUG_RE)]


class ValidateRequest(BaseModel):
    cluster_id: int
    expr: str


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


async def _get_cluster_or_404(session: AsyncSession, cluster_id: int) -> Cluster:
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")
    return cluster


async def _validate_expr(
    cluster: Cluster, http_client: httpx.AsyncClient, expr: str
) -> dict[str, Any]:
    """Shared by create/update (which must reject an invalid expr before
    touching k8s at all) and the standalone /rules/validate endpoint.

    Prometheus being unreachable degrades to a 503 here rather than "valid":
    without it we simply cannot know whether the expression parses.
    """
    try:
        return await PrometheusClient(cluster, http_client).validate_query(expr)
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.get("")
async def list_rules(
    team_id: int,
    cluster_id: int = Query(...),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _member: User = Depends(require_team_role("member")),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)
    cluster = await _get_cluster_or_404(session, cluster_id)

    try:
        raw_rules = await k8s.list_rules(cluster, team.id)
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    rules = [parse_prometheus_rule(obj) for obj in raw_rules]

    warning: str | None = None
    try:
        health_map = await PrometheusClient(cluster, http_client).get_rules_health()
    except PrometheusUnavailableError as exc:
        # Health is best-effort: a Prometheus outage shouldn't hide the rule
        # list itself, just degrade every rule's health to "unknown".
        health_map = {}
        warning = f"prometheus unavailable, rule health is unknown: {exc}"

    for rule in rules:
        health = health_map.get(rule["alert_name"])
        rule["health"] = health["health"] if health else "unknown"
        rule["state"] = health["state"] if health else None
        rule["last_error"] = health["last_error"] if health else None

    result: dict[str, Any] = {"rules": rules}
    if warning:
        result["warning"] = warning
    return result


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rule(
    team_id: int,
    body: RuleWrite,
    cluster_id: int = Query(...),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    actor: User = Depends(require_team_role("member")),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)
    cluster = await _get_cluster_or_404(session, cluster_id)

    validation = await _validate_expr(cluster, http_client, body.expr)
    if not validation["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid PromQL expression: {validation['error']}",
        )

    manifest = build_prometheus_rule(team, body)
    try:
        created = await k8s.create_rule(cluster, manifest)
    except RuleConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="rule.create",
        object_type="prometheus_rule",
        object_ref=manifest["metadata"]["name"],
        detail={"cluster_id": cluster.id, "slug": body.slug},
    )
    await session.commit()

    return parse_prometheus_rule(created)


class RuleImportRequest(BaseModel):
    data: dict[str, Any]
    target_cluster_id: int
    conflict_strategy: ConflictStrategy
    dry_run: bool = False


# Registered ahead of GET /{slug} below: "export" is itself a valid RULE_SLUG_RE
# string, and Starlette matches routes in registration order against the raw
# path shape (the {slug} pattern constraint is only enforced once a route has
# already matched) -- so this must come first or GET .../rules/export would
# be swallowed by GET .../rules/{slug} instead.
@router.get("/export")
async def export_rules(
    team_id: int,
    cluster_id: int = Query(...),
    slugs: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    actor: User = Depends(require_team_role("member")),
) -> Response:
    """A portable JSON envelope of this team's rules on one cluster --
    ownership metadata stripped (already true of `parse_prometheus_rule`'s
    output), ready to hand to POST .../rules/import against any team/cluster.
    """
    team = await _get_team_or_404(session, team_id)
    cluster = await _get_cluster_or_404(session, cluster_id)

    try:
        raw_rules = await k8s.list_rules(cluster, team.id)
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    parsed = [parse_prometheus_rule(obj) for obj in raw_rules]
    if slugs:
        wanted = {s.strip() for s in slugs.split(",") if s.strip()}
        parsed = [r for r in parsed if r["slug"] in wanted]

    envelope = build_export_envelope(team, cluster, parsed)

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="rules.export",
        object_type="prometheus_rule",
        object_ref=cluster.name,
        detail={"count": len(parsed)},
    )
    await session.commit()

    filename = f"kam-rules-{team.slug}-{cluster.name}.json"
    return Response(
        content=json.dumps(envelope, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/import")
async def import_rules(
    team_id: int,
    body: RuleImportRequest,
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    # A write, unlike every other rules/* endpoint here (which accept
    # 'member') -- importing can create/overwrite rules across an entire
    # cluster in one call, so it's owner-gated.
    actor: User = Depends(require_team_role("owner")),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)
    target_cluster = await _get_cluster_or_404(session, body.target_cluster_id)

    try:
        envelope = parse_envelope(body.data)
    except UnsupportedExportVersion as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    runner = plan_import if body.dry_run else execute_import
    try:
        verdicts = await runner(
            k8s,
            http_client,
            team=team,
            target_cluster=target_cluster,
            entries=envelope.entries,
            conflict_strategy=body.conflict_strategy,
        )
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    summary = {"created": 0, "skipped": 0, "overwritten": 0, "renamed": 0, "failed": 0}
    for verdict in verdicts:
        summary[verdict.action] += 1

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="rules.import",
        object_type="prometheus_rule",
        object_ref=target_cluster.name,
        detail={"summary": summary, "dry_run": body.dry_run},
    )
    await session.commit()

    return {"verdicts": [v.to_dict() for v in verdicts], "summary": summary}


@router.get("/{slug}")
async def get_rule(
    team_id: int,
    slug: SlugParam,
    cluster_id: int = Query(...),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    _member: User = Depends(require_team_role("member")),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)
    cluster = await _get_cluster_or_404(session, cluster_id)
    name = rule_object_name(team.id, slug)

    obj = await _get_owned_rule_or_404(k8s, cluster, name, team.id)
    return parse_prometheus_rule(obj)


@router.put("/{slug}")
async def update_rule(
    team_id: int,
    slug: SlugParam,
    body: RuleWrite,
    cluster_id: int = Query(...),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    actor: User = Depends(require_team_role("member")),
) -> dict[str, Any]:
    team = await _get_team_or_404(session, team_id)
    cluster = await _get_cluster_or_404(session, cluster_id)
    name = rule_object_name(team.id, slug)

    # 404 for a genuinely absent rule; the ownership guard below (re-checked
    # independently inside k8s.replace_rule) is what turns "exists but not
    # ours" into 403.
    await _get_owned_rule_or_404(k8s, cluster, name, team.id, forbidden_as_404=True)

    validation = await _validate_expr(cluster, http_client, body.expr)
    if not validation["valid"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid PromQL expression: {validation['error']}",
        )

    manifest = build_prometheus_rule(team, body)
    # The path slug is authoritative for identity; renaming via PUT isn't
    # supported in this phase (the frontend disables the slug field on edit).
    manifest["metadata"]["name"] = name

    try:
        updated = await k8s.replace_rule(cluster, name, team.id, manifest)
    except RuleForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuleUpdateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="동시 수정 충돌") from exc
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="rule.update",
        object_type="prometheus_rule",
        object_ref=name,
        detail={"cluster_id": cluster.id},
    )
    await session.commit()

    return parse_prometheus_rule(updated)


@router.delete("/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_rule(
    team_id: int,
    slug: SlugParam,
    cluster_id: int = Query(...),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    actor: User = Depends(require_team_role("member")),
) -> None:
    team = await _get_team_or_404(session, team_id)
    cluster = await _get_cluster_or_404(session, cluster_id)
    name = rule_object_name(team.id, slug)

    await _get_owned_rule_or_404(k8s, cluster, name, team.id, forbidden_as_404=True)

    try:
        await k8s.delete_rule(cluster, name, team.id)
    except RuleForbiddenError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except RuleUpdateConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="동시 수정 충돌") from exc
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team.id,
        action="rule.delete",
        object_type="prometheus_rule",
        object_ref=name,
        detail={"cluster_id": cluster.id},
    )
    await session.commit()


async def _get_owned_rule_or_404(
    k8s: K8sClientFactory,
    cluster: Cluster,
    name: str,
    team_id: int,
    *,
    forbidden_as_404: bool = False,
) -> dict[str, Any]:
    """Fetch a rule, 404-ing if it doesn't exist.

    By default also 404s (rather than 403s) when it exists but isn't owned
    by this team, so GET never confirms the existence of another team's
    rule. `forbidden_as_404=True` narrows that to "genuinely absent only" --
    used by PUT/DELETE, which want a real 403 for "exists, not yours" (the
    guard itself is still enforced independently inside k8s.py).
    """
    try:
        obj = await k8s.get_rule(cluster, name)
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule not found")

    if not forbidden_as_404:
        labels = (obj.get("metadata") or {}).get("labels") or {}
        owned = (
            labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE
            and labels.get(TEAM_ID_LABEL) == str(team_id)
        )
        if not owned:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="rule not found")

    return obj


@validate_router.post("/validate")
async def validate_rule_expr(
    body: ValidateRequest,
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, body.cluster_id)
    return await _validate_expr(cluster, http_client, body.expr)
