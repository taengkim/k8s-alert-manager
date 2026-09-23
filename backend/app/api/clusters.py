"""Cluster CRUD (admin-only writes), the plain listing every authenticated
user can see, per-cluster health, and the k8s namespaces lookup used by the
rule builder.

Field visibility: every authenticated user can see id/name/display_name/
enabled/health/heartbeat_*; only an admin additionally sees connection
config (prometheus_url/alertmanager_url/grafana_url/rules_namespace/
k8s_auth_kind/k8s_api_url/heartbeat config detail). Credentials and the
webhook token hash are never returned to anyone, at any role -- the
plaintext webhook token is only ever visible once, in the POST (create) or
PATCH (rotate) response that just (re)generated it.
"""

import json
import secrets
from typing import Annotated, Any, Literal

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_cluster_health_cache,
    get_current_user,
    get_k8s_factory,
    require_admin,
)
from app.config import get_settings
from app.db import get_session
from app.models.alert import AlertEvent
from app.models.cluster import Cluster
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.security import encrypt_str, hash_token
from app.services import audit
from app.services.cluster_health import ClusterHealthCache
from app.services.k8s import K8sBadRequestError, K8sClientFactory, K8sUnavailableError

router = APIRouter(prefix="/api/v1/clusters", tags=["clusters"])
namespaces_router = APIRouter(prefix="/api/v1", tags=["clusters"])

# No leading/trailing hyphen, max 63 chars -- mirrors app.api.teams.SLUG_RE;
# a cluster's `name` plays the same "immutable slug" role a team's does.
NAME_RE = r"^[a-z0-9][a-z0-9-]{1,62}$"
NameParam = Annotated[str, Field(pattern=NAME_RE)]

AuthKind = Literal["incluster", "kubeconfig", "token"]


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


class ClusterCreate(BaseModel):
    name: NameParam
    display_name: str = Field(min_length=1)
    k8s_auth_kind: AuthKind = "kubeconfig"
    k8s_api_url: str | None = None
    # 'kubeconfig' auth: a raw kubeconfig YAML string (or omitted, meaning
    # "use the host's default kubeconfig" -- dev convenience, see
    # K8sClientFactory._build_unsafe). 'token' auth: {"token": str,
    # "ca_cert": str | None}. 'incluster' auth: must be omitted.
    credentials: Any = None
    prometheus_url: str = Field(min_length=1)
    alertmanager_url: str = Field(min_length=1)
    grafana_url: str | None = None
    rules_namespace: str = "kam-rules"
    heartbeat_enabled: bool = True
    heartbeat_alertname: str = "Watchdog"
    heartbeat_timeout_seconds: int = Field(default=600, gt=0)
    heartbeat_team_id: int | None = None


class ClusterUpdate(BaseModel):
    """All fields optional -- only ones present in the request body (see
    `model_fields_set` below) are applied. `name` is accepted only so a
    client attempting to change it gets a clear 422 rather than the field
    being silently ignored.
    """

    name: str | None = None
    display_name: str | None = Field(default=None, min_length=1)
    k8s_auth_kind: AuthKind | None = None
    k8s_api_url: str | None = None
    credentials: Any = None
    prometheus_url: str | None = Field(default=None, min_length=1)
    alertmanager_url: str | None = Field(default=None, min_length=1)
    grafana_url: str | None = None
    rules_namespace: str | None = None
    enabled: bool | None = None
    heartbeat_enabled: bool | None = None
    heartbeat_alertname: str | None = None
    heartbeat_timeout_seconds: int | None = Field(default=None, gt=0)
    heartbeat_team_id: int | None = None
    rotate_webhook_token: bool = False


def _validate_and_encrypt_credentials(k8s_auth_kind: str, credentials: Any) -> str | None:
    """Shape-validates `credentials` against `k8s_auth_kind` and returns the
    encrypted column value to store (or None). Raises 422 on a mismatched
    shape -- this always runs before anything touches the database, so a
    bad payload never partially writes a cluster row.
    """
    if k8s_auth_kind == "incluster":
        if credentials is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="incluster auth kind does not accept credentials",
            )
        return None

    if k8s_auth_kind == "kubeconfig":
        if credentials is None:
            return None
        if not isinstance(credentials, str):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="kubeconfig credentials must be a raw kubeconfig YAML string",
            )
        try:
            yaml.safe_load(credentials)
        except yaml.YAMLError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"invalid kubeconfig YAML: {exc}",
            ) from exc
        return encrypt_str(credentials)

    if k8s_auth_kind == "token":
        if not isinstance(credentials, dict) or not credentials.get("token"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="token auth kind requires credentials={token, ca_cert?}",
            )
        stored = {"token": credentials["token"], "ca_cert": credentials.get("ca_cert")}
        return encrypt_str(json.dumps(stored))

    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"unknown k8s_auth_kind '{k8s_auth_kind}'",
    )


def _build_am_snippet(token: str) -> str:
    """The Alertmanager receiver config snippet an admin pastes into their
    cluster's Alertmanager config after creating/rotating a cluster --
    mirrors the shape `dev/kube-prometheus-values.yaml` already uses for
    the dev kind cluster. Returned only alongside a freshly (re)generated
    plaintext token -- never reconstructable afterwards, since only the
    token's hash is stored.
    """
    settings = get_settings()
    webhook_url = f"{settings.webhook_base_url.rstrip('/')}/api/v1/webhook/alertmanager"
    return (
        "receivers:\n"
        "  - name: kam-webhook\n"
        "    webhook_configs:\n"
        f"      - url: {webhook_url}\n"
        "        send_resolved: true\n"
        "        http_config:\n"
        "          authorization:\n"
        "            type: Bearer\n"
        f"            credentials: {token}\n"
    )


def _serialize_cluster(
    cluster: Cluster, *, is_admin: bool, health: dict[str, Any] | None
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": cluster.id,
        "name": cluster.name,
        "display_name": cluster.display_name,
        "enabled": cluster.enabled,
        "health": health,
        "heartbeat_state": cluster.heartbeat_state,
        "last_heartbeat_at": cluster.last_heartbeat_at,
    }
    if is_admin:
        out.update(
            {
                "k8s_auth_kind": cluster.k8s_auth_kind,
                "k8s_api_url": cluster.k8s_api_url,
                "prometheus_url": cluster.prometheus_url,
                "alertmanager_url": cluster.alertmanager_url,
                "grafana_url": cluster.grafana_url,
                "rules_namespace": cluster.rules_namespace,
                # Not in the brief's headline admin-field list, but the
                # admin clusters Drawer needs these to prefill an edit form
                # -- credentials/webhook_token_hash themselves are still
                # never included, at any role.
                "heartbeat_enabled": cluster.heartbeat_enabled,
                "heartbeat_alertname": cluster.heartbeat_alertname,
                "heartbeat_timeout_seconds": cluster.heartbeat_timeout_seconds,
                "heartbeat_team_id": cluster.heartbeat_team_id,
            }
        )
    return out


async def _get_cluster_or_404(session: AsyncSession, cluster_id: int) -> Cluster:
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")
    return cluster


@router.get("")
async def list_clusters(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    health_cache: ClusterHealthCache = Depends(get_cluster_health_cache),
) -> list[dict[str, Any]]:
    """Every authenticated user's cluster listing. `health` is a cache-only
    peek (see ClusterHealthCache.peek) -- never triggers a live probe, so
    this stays cheap regardless of how many clusters exist; it's `None`
    until something (the admin clusters page's own poll, or a direct
    `GET .../health` call) has populated the cache at least once.
    """
    result = await session.execute(select(Cluster))
    return [
        _serialize_cluster(c, is_admin=user.is_admin, health=health_cache.peek(c.id))
        for c in result.scalars().all()
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_cluster(
    body: ClusterCreate,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if body.heartbeat_team_id is not None:
        team = await session.get(Team, body.heartbeat_team_id)
        if team is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="unknown heartbeat_team_id",
            )

    credentials_encrypted = _validate_and_encrypt_credentials(body.k8s_auth_kind, body.credentials)
    token = secrets.token_urlsafe(32)

    cluster = Cluster(
        name=body.name,
        display_name=body.display_name,
        k8s_auth_kind=body.k8s_auth_kind,
        k8s_api_url=body.k8s_api_url,
        credentials_encrypted=credentials_encrypted,
        prometheus_url=body.prometheus_url,
        alertmanager_url=body.alertmanager_url,
        grafana_url=body.grafana_url,
        rules_namespace=body.rules_namespace,
        webhook_token_hash=hash_token(token),
        heartbeat_enabled=body.heartbeat_enabled,
        heartbeat_alertname=body.heartbeat_alertname,
        heartbeat_timeout_seconds=body.heartbeat_timeout_seconds,
        heartbeat_team_id=body.heartbeat_team_id,
    )
    session.add(cluster)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="cluster name already exists"
        ) from exc

    await audit.log(
        session,
        user_id=actor.id,
        team_id=None,
        action="cluster.create",
        object_type="cluster",
        object_ref=cluster.name,
    )
    await session.commit()
    await session.refresh(cluster)

    result = _serialize_cluster(cluster, is_admin=True, health=None)
    result["webhook_token"] = token
    result["am_config_snippet"] = _build_am_snippet(token)
    return result


_PLAIN_UPDATE_FIELDS = (
    "display_name",
    "k8s_api_url",
    "prometheus_url",
    "alertmanager_url",
    "grafana_url",
    "rules_namespace",
    "enabled",
    "heartbeat_enabled",
    "heartbeat_alertname",
    "heartbeat_timeout_seconds",
    "heartbeat_team_id",
)


@router.patch("/{cluster_id}")
async def update_cluster(
    cluster_id: int,
    body: ClusterUpdate,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, cluster_id)
    fields_set = body.model_fields_set

    if "name" in fields_set and body.name is not None and body.name != cluster.name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="cluster name (slug) cannot be changed",
        )

    if "heartbeat_team_id" in fields_set and body.heartbeat_team_id is not None:
        team = await session.get(Team, body.heartbeat_team_id)
        if team is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="unknown heartbeat_team_id",
            )

    changed = False
    for field in _PLAIN_UPDATE_FIELDS:
        if field in fields_set:
            setattr(cluster, field, getattr(body, field))
            changed = True

    # A kind change with no new credentials, while old ones are still on
    # file, would otherwise silently 200 into an unusable config: the stored
    # ciphertext stays whatever shape the *old* kind expected (a kubeconfig
    # YAML string, say), read back under a k8s_auth_kind that expects
    # something else (a {token, ca_cert} JSON blob) -- POST already refuses
    # this combination outright (credentials required per auth kind); PATCH
    # must refuse it too rather than deferring the failure to the next time
    # something actually tries to build a k8s client for this cluster.
    # `credentials: null` is how an admin explicitly clears the old value
    # instead -- that still reaches _validate_and_encrypt_credentials below
    # ("credentials" is in fields_set) and is accepted for every kind that
    # doesn't itself require credentials.
    kind_is_changing = "k8s_auth_kind" in fields_set and body.k8s_auth_kind != cluster.k8s_auth_kind
    if (
        kind_is_changing
        and "credentials" not in fields_set
        and cluster.credentials_encrypted is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="새 인증 방식의 자격증명을 함께 제공하세요",
        )

    # k8s_auth_kind and credentials are handled together: re-validating
    # credentials against whichever auth kind is effective *after* this
    # update (a PATCH changing both at once must validate against the new
    # kind, not the cluster's old one).
    if "k8s_auth_kind" in fields_set or "credentials" in fields_set:
        effective_auth_kind = (
            body.k8s_auth_kind if "k8s_auth_kind" in fields_set else cluster.k8s_auth_kind
        )
        if "k8s_auth_kind" in fields_set:
            cluster.k8s_auth_kind = body.k8s_auth_kind
        if "credentials" in fields_set:
            cluster.credentials_encrypted = _validate_and_encrypt_credentials(
                effective_auth_kind, body.credentials
            )
        changed = True

    if changed:
        await audit.log(
            session,
            user_id=actor.id,
            team_id=None,
            action="cluster.update",
            object_type="cluster",
            object_ref=cluster.name,
        )

    rotated_token: str | None = None
    if body.rotate_webhook_token:
        rotated_token = secrets.token_urlsafe(32)
        cluster.webhook_token_hash = hash_token(rotated_token)
        await audit.log(
            session,
            user_id=actor.id,
            team_id=None,
            action="cluster.rotate_token",
            object_type="cluster",
            object_ref=cluster.name,
        )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="conflicting cluster field"
        ) from exc
    await session.refresh(cluster)

    result = _serialize_cluster(cluster, is_admin=True, health=None)
    if rotated_token is not None:
        result["webhook_token"] = rotated_token
        result["am_config_snippet"] = _build_am_snippet(rotated_token)
    return result


@router.delete("/{cluster_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cluster(
    cluster_id: int,
    actor: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Hard-deletes a cluster with no alert history; 409s instead when it
    has any (`alert_events.cluster_id` is a plain, non-cascading FK, so a
    forced delete would fail at the database level anyway -- this gives the
    friendly, actionable message instead of a raw integrity error).
    """
    cluster = await _get_cluster_or_404(session, cluster_id)

    has_history = (
        await session.execute(
            select(AlertEvent.id).where(AlertEvent.cluster_id == cluster.id).limit(1)
        )
    ).scalar_one_or_none()
    if has_history is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이력이 있는 클러스터는 비활성화하세요",
        )

    cluster_name = cluster.name
    await session.delete(cluster)
    try:
        await session.flush()
    except IntegrityError:
        # Defensive: catches e.g. a lingering silence_audit row the
        # pre-check above doesn't look at.
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이력이 있는 클러스터는 비활성화하세요",
        ) from None

    await audit.log(
        session,
        user_id=actor.id,
        team_id=None,
        action="cluster.delete",
        object_type="cluster",
        object_ref=cluster_name,
    )
    await session.commit()


async def _require_health_viewer(cluster: Cluster, user: User, session: AsyncSession) -> None:
    """Health is visible to an admin, or an owner-role member of the
    cluster's heartbeat-attributed team -- a cluster with no
    `heartbeat_team_id` set is admin-only. This mirrors
    `deps.require_team_role('owner')`'s membership check, but keyed off a
    cluster's own team attribution rather than a `{team_id}` path param,
    since health is looked up by cluster id.
    """
    if user.is_admin:
        return
    if cluster.heartbeat_team_id is not None:
        result = await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == cluster.heartbeat_team_id,
                TeamMembership.user_id == user.id,
                TeamMembership.role == "owner",
            )
        )
        if result.scalar_one_or_none() is not None:
            return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


@router.get("/{cluster_id}/health")
async def get_cluster_health(
    cluster_id: int,
    refresh: bool = Query(default=False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
    health_cache: ClusterHealthCache = Depends(get_cluster_health_cache),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, cluster_id)
    await _require_health_viewer(cluster, user, session)
    return await health_cache.get(cluster, k8s_factory=k8s, http_client=http_client, refresh=refresh)


@namespaces_router.get("/namespaces")
async def list_namespaces(
    cluster_id: int = Query(...),
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    k8s: K8sClientFactory = Depends(get_k8s_factory),
) -> list[str]:
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")

    try:
        return await k8s.list_namespaces(cluster)
    except K8sBadRequestError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except K8sUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
