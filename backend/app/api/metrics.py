"""Metrics-browser API: server-guarded proxy over a cluster's Prometheus for
the threshold builder's metric autocomplete, label filters, and preview
chart.

Deliberately not a raw passthrough -- every endpoint enforces its own
guardrails (result caps, a 60s name-list cache, a 7-day range cap, a
500-point step floor) server-side, since the frontend's own limits are only
a convenience, not something a caller can be trusted to respect.
"""

import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_session
from app.models.cluster import Cluster
from app.models.user import User
from app.services.prometheus import (
    PrometheusClient,
    PrometheusQueryError,
    PrometheusUnavailableError,
)

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])

DEFAULT_NAMES_LIMIT = 50
MAX_NAMES_LIMIT = 200
NAMES_CACHE_TTL_SECONDS = 60.0

MAX_RANGE_SECONDS = 7 * 24 * 3600
MAX_POINTS = 500
MIN_STEP_SECONDS = 15.0
MAX_SERIES = 50
MAX_INSTANT_SAMPLES = 50

# Prometheus label-name syntax: used both for the metric name itself and for
# label keys. Interpolated straight into a URL path segment
# (/api/v1/label/{label}/values), so this also guards against path
# injection -- reject anything that doesn't match before it ever reaches
# httpx.
_LABEL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

# {cluster_id: (fetched_at_monotonic, names)} -- process-local, not shared
# across workers. Simple by design (see task brief): a stale cache just
# means a newly-scraped metric takes up to 60s to appear in autocomplete.
_names_cache: dict[int, tuple[float, list[str]]] = {}


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


async def _get_cluster_or_404(session: AsyncSession, cluster_id: int) -> Cluster:
    cluster = await session.get(Cluster, cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")
    return cluster


def _require_label_name(value: str, *, field: str) -> None:
    if not _LABEL_NAME_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"invalid {field}: '{value}'",
        )


async def _fetch_names_cached(cluster: Cluster, http_client: httpx.AsyncClient) -> list[str]:
    now = time.monotonic()
    cached = _names_cache.get(cluster.id)
    if cached is not None and now - cached[0] < NAMES_CACHE_TTL_SECONDS:
        return cached[1]

    names = await PrometheusClient(cluster, http_client).label_name_values()
    _names_cache[cluster.id] = (now, names)
    return names


@router.get("/names")
async def list_metric_names(
    cluster_id: int = Query(...),
    search: str | None = Query(default=None),
    limit: int = Query(default=DEFAULT_NAMES_LIMIT, ge=1, le=MAX_NAMES_LIMIT),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, cluster_id)
    try:
        names = await _fetch_names_cached(cluster, http_client)
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    if search:
        needle = search.lower()
        names = [name for name in names if needle in name.lower()]

    return {"names": names[:limit]}


@router.get("/metadata")
async def get_metric_metadata(
    cluster_id: int = Query(...),
    metric: str = Query(...),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, cluster_id)
    try:
        return await PrometheusClient(cluster, http_client).metric_metadata(metric)
    except PrometheusQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.get("/labels")
async def list_metric_labels(
    cluster_id: int = Query(...),
    metric: str = Query(...),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, cluster_id)
    try:
        labels = await PrometheusClient(cluster, http_client).labels_for_metric(metric)
    except PrometheusQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"labels": labels}


@router.get("/label-values")
async def list_label_values(
    cluster_id: int = Query(...),
    metric: str = Query(...),
    label: str = Query(...),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    _require_label_name(label, field="label")
    cluster = await _get_cluster_or_404(session, cluster_id)
    try:
        values = await PrometheusClient(cluster, http_client).label_values_for_metric(
            label, metric
        )
    except PrometheusQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return {"values": values}


class InstantQueryRequest(BaseModel):
    cluster_id: int
    query: str


def _coerce_sample_value(raw: Any) -> float | None:
    if not isinstance(raw, list) or len(raw) != 2:
        return None
    try:
        return float(raw[1])
    except (TypeError, ValueError):
        return None


@router.post("/query")
async def instant_query(
    body: InstantQueryRequest,
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, body.cluster_id)
    try:
        result = await PrometheusClient(cluster, http_client).instant_query(body.query)
    except PrometheusQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    raw_result = result.get("result") or []
    samples = [
        {"labels": dict(item.get("metric") or {}), "value": _coerce_sample_value(item.get("value"))}
        for item in raw_result[:MAX_INSTANT_SAMPLES]
    ]
    return {
        "result_type": result.get("resultType", ""),
        "series_count": len(raw_result),
        "samples": samples,
    }


class QueryRangeRequest(BaseModel):
    cluster_id: int
    query: str
    start: float
    end: float
    step: float | None = None


def _resolve_step(range_seconds: float, requested_step: float | None) -> float:
    if requested_step is None:
        return max(range_seconds / 250, MIN_STEP_SECONDS)
    # An explicit step is only ever raised, never lowered: it must not
    # produce more than MAX_POINTS points over the requested range.
    min_step_for_cap = range_seconds / MAX_POINTS
    return max(requested_step, min_step_for_cap)


@router.post("/query_range")
async def query_range(
    body: QueryRangeRequest,
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
    _user: User = Depends(get_current_user),
) -> dict[str, Any]:
    cluster = await _get_cluster_or_404(session, body.cluster_id)

    range_seconds = body.end - body.start
    if range_seconds <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="end must be after start"
        )
    if range_seconds > MAX_RANGE_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"range exceeds the {MAX_RANGE_SECONDS} second (7 day) maximum",
        )

    step = _resolve_step(range_seconds, body.step)

    try:
        result = await PrometheusClient(cluster, http_client).query_range(
            body.query, body.start, body.end, step
        )
    except PrometheusQueryError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except PrometheusUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    matrix = result.get("result") or []
    truncated = len(matrix) > MAX_SERIES
    series = []
    for item in matrix[:MAX_SERIES]:
        points: list[list[float | None]] = []
        for point in item.get("values") or []:
            if not isinstance(point, list) or len(point) != 2:
                continue
            ts, raw_value = point
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                value = None
            points.append([ts, value])
        series.append({"labels": dict(item.get("metric") or {}), "points": points})

    return {
        "series": series,
        "truncated": truncated,
        "total_series": len(matrix),
        "step_used": step,
    }
