"""Live alert fan-out endpoint: proxies each enabled cluster's Alertmanager,
scoped by team and filtered server-side.
"""

import asyncio
import logging
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_session
from app.models.cluster import Cluster
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.services.alertmanager import AlertmanagerClient, AlertmanagerUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


async def _resolve_team_scope(
    team_id: int | None, user: User, session: AsyncSession
) -> Team | None:
    """Authorize the requested team scope and return it (or None for admin's
    unscoped "all alerts" view).

    Non-admins must supply a `team_id` they belong to. Admins may omit it to
    see every alert, including ones without a `kam_team` label.
    """
    if team_id is None:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
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


def _flatten(cluster_name: str, raw: dict[str, Any]) -> dict[str, Any]:
    labels = raw.get("labels") or {}
    status_obj = raw.get("status") or {}
    return {
        "fingerprint": raw.get("fingerprint"),
        "alertname": labels.get("alertname", ""),
        "severity": labels.get("severity", ""),
        "namespace": labels.get("namespace", ""),
        "cluster": cluster_name,
        "state": status_obj.get("state", ""),
        "labels": labels,
        "annotations": raw.get("annotations") or {},
        "starts_at": raw.get("startsAt"),
        "generator_url": raw.get("generatorURL"),
        "silenced_by": status_obj.get("silencedBy") or [],
    }


async def _fetch_cluster_alerts(
    cluster: Cluster, http_client: httpx.AsyncClient
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Fetch+flatten one cluster's alerts. Any failure -- connect/timeout,
    a malformed (non-JSON or non-list) response body, whatever -- degrades
    to an errors[] entry for this cluster rather than failing the whole
    fan-out (asyncio.gather has no return_exceptions, so an uncaught
    exception here would 500 the entire request and blank out every other
    cluster's alerts too).
    """
    client = AlertmanagerClient(cluster, http_client)
    try:
        raw_alerts = await client.get_alerts()
        return cluster.name, [_flatten(cluster.name, a) for a in raw_alerts], None
    except AlertmanagerUnavailableError as exc:
        return cluster.name, [], str(exc)
    except Exception:
        logger.exception(
            "unexpected error fetching alerts for cluster '%s'", cluster.name
        )
        return cluster.name, [], "unexpected error fetching alerts"


@router.get("/live")
async def get_live_alerts(
    team_id: int | None = Query(default=None),
    severity: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    search: str | None = Query(default=None),
    state: Literal["active", "suppressed"] | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    team = await _resolve_team_scope(team_id, user, session)

    clusters = (
        (await session.execute(select(Cluster).where(Cluster.enabled.is_(True))))
        .scalars()
        .all()
    )

    fetch_results = await asyncio.gather(
        *(_fetch_cluster_alerts(cluster, http_client) for cluster in clusters)
    )

    alerts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for cluster_name, cluster_alerts, error in fetch_results:
        if error is not None:
            errors.append({"cluster": cluster_name, "message": error})
            continue
        alerts.extend(cluster_alerts)

    if team is not None:
        alerts = [a for a in alerts if a["labels"].get("kam_team") == team.slug]

    if severity:
        # "none" is a synthetic value the frontend offers for alerts with no
        # severity label at all (labels.get("severity", "") flattens to "").
        wanted = {s.strip() for s in severity.split(",") if s.strip()}
        alerts = [a for a in alerts if (a["severity"] or "none") in wanted]

    if namespace:
        alerts = [a for a in alerts if a["namespace"] == namespace]

    if state:
        alerts = [a for a in alerts if a["state"] == state]

    if search:
        needle = search.lower()
        alerts = [a for a in alerts if needle in a["alertname"].lower()]

    return {"alerts": alerts, "errors": errors}
