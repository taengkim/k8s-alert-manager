"""Prometheus API client: PromQL validation and alerting-rule health.

Mirrors `AlertmanagerClient`'s shape (a shared `httpx.AsyncClient`, a typed
unavailable error) but talks to a cluster's Prometheus instead of its
Alertmanager.
"""

from typing import Any

import httpx

from app.models.cluster import Cluster

DEFAULT_TIMEOUT = httpx.Timeout(10.0)


class PrometheusUnavailableError(Exception):
    """Raised when a cluster's Prometheus can't be reached in time, or
    returns something other than a well-formed 200/400."""


class PrometheusQueryError(Exception):
    """Raised when Prometheus rejects a query or label matcher as malformed
    (a 400 bad_data response) -- the caller's own input, as opposed to the
    service being unreachable. The API layer maps this to a 422 carrying
    this message, the same treatment `validate_query`'s 400 branch already
    gives an invalid expression."""


# Metrics-browser endpoints (names/metadata/labels/label-values/query/
# query_range) use a longer timeout than the 10s default: label-values
# queries over a high-cardinality metric or a wide query_range can
# legitimately take longer than a simple /format_query round trip.
METRICS_TIMEOUT = httpx.Timeout(15.0)


class PrometheusClient:
    def __init__(self, cluster: Cluster, http_client: httpx.AsyncClient) -> None:
        self._cluster = cluster
        self._http = http_client

    async def _get_json(
        self, path: str, params: dict[str, Any], *, timeout: httpx.Timeout = METRICS_TIMEOUT
    ) -> dict[str, Any]:
        """Shared GET-and-decode for the metrics-browser endpoints below.

        Mirrors the error-mapping shape of `validate_query`/`get_rules_health`:
        a transport failure or non-200/400 status is "unavailable" (503 at
        the API layer); a 400 is the caller's own bad input, raised as
        `PrometheusQueryError` (422 at the API layer) rather than folded
        into the same unavailable bucket.
        """
        url = f"{self._cluster.prometheus_url}{path}"
        try:
            response = await self._http.get(url, params=params, timeout=timeout)
        except httpx.TransportError as exc:
            raise PrometheusUnavailableError(
                f"cannot reach prometheus for cluster '{self._cluster.name}': {exc}"
            ) from exc

        if response.status_code == 400:
            raise PrometheusQueryError(_extract_error(response))
        if response.status_code != 200:
            raise PrometheusUnavailableError(
                f"prometheus for cluster '{self._cluster.name}' returned {response.status_code}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise PrometheusUnavailableError(
                f"prometheus for cluster '{self._cluster.name}' returned a non-JSON body"
            ) from exc
        if not isinstance(data, dict):
            raise PrometheusUnavailableError(
                f"prometheus for cluster '{self._cluster.name}' returned an unexpected body"
            )
        return data

    async def label_name_values(self) -> list[str]:
        """All metric names known to this cluster's Prometheus (the raw,
        unfiltered list -- callers apply their own search/limit)."""
        data = await self._get_json("/api/v1/label/__name__/values", {})
        names = data.get("data")
        return names if isinstance(names, list) else []

    async def metric_metadata(self, metric: str) -> dict[str, Any]:
        """First metadata entry ({type, help, unit}) for `metric`, or {} if
        Prometheus has none on file for it."""
        data = await self._get_json("/api/v1/metadata", {"metric": metric})
        entries = (data.get("data") or {}).get(metric) or []
        return entries[0] if entries else {}

    async def labels_for_metric(self, metric: str) -> list[str]:
        """Label keys that appear on series matching `metric`, excluding
        the synthetic `__name__` label."""
        data = await self._get_json("/api/v1/labels", {"match[]": metric})
        values = data.get("data")
        if not isinstance(values, list):
            return []
        return [label for label in values if label != "__name__"]

    async def label_values_for_metric(self, label: str, metric: str) -> list[str]:
        data = await self._get_json(
            f"/api/v1/label/{label}/values", {"match[]": metric}
        )
        values = data.get("data")
        return values[:200] if isinstance(values, list) else []

    async def instant_query(self, query: str) -> dict[str, Any]:
        """Raw `data` object from an instant `/api/v1/query`: {resultType, result}."""
        data = await self._get_json("/api/v1/query", {"query": query})
        result = data.get("data")
        return result if isinstance(result, dict) else {}

    async def query_range(
        self, query: str, start: float, end: float, step: float
    ) -> dict[str, Any]:
        """Raw `data` object from `/api/v1/query_range`: {resultType, result}.

        Guardrails (range cap, step computation, series/point truncation)
        are the API layer's job -- this is a thin, untruncated pass-through
        of whatever Prometheus returns for the given start/end/step.
        """
        data = await self._get_json(
            "/api/v1/query_range",
            {"query": query, "start": start, "end": end, "step": step},
        )
        result = data.get("data")
        return result if isinstance(result, dict) else {}

    async def validate_query(self, expr: str) -> dict[str, Any]:
        url = f"{self._cluster.prometheus_url}/api/v1/format_query"
        try:
            response = await self._http.post(url, data={"query": expr}, timeout=DEFAULT_TIMEOUT)
        except httpx.TransportError as exc:
            raise PrometheusUnavailableError(
                f"cannot reach prometheus for cluster '{self._cluster.name}': {exc}"
            ) from exc

        if response.status_code == 200:
            return {"valid": True, "error": None}
        if response.status_code == 400:
            return {"valid": False, "error": _extract_error(response)}

        raise PrometheusUnavailableError(
            f"prometheus for cluster '{self._cluster.name}' returned {response.status_code}"
        )

    async def get_rules_health(self) -> dict[str, dict[str, Any]]:
        url = f"{self._cluster.prometheus_url}/api/v1/rules"
        try:
            response = await self._http.get(
                url, params={"type": "alert"}, timeout=DEFAULT_TIMEOUT
            )
            response.raise_for_status()
        except httpx.TransportError as exc:
            raise PrometheusUnavailableError(
                f"cannot reach prometheus for cluster '{self._cluster.name}': {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise PrometheusUnavailableError(
                f"prometheus for cluster '{self._cluster.name}' returned "
                f"{exc.response.status_code}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise PrometheusUnavailableError(
                f"prometheus for cluster '{self._cluster.name}' returned a non-JSON body"
            ) from exc

        if not isinstance(data, dict):
            raise PrometheusUnavailableError(
                f"prometheus for cluster '{self._cluster.name}' returned an unexpected body"
            )

        health: dict[str, dict[str, Any]] = {}
        for group in (data.get("data") or {}).get("groups") or []:
            for rule in group.get("rules") or []:
                name = rule.get("name")
                if not name:
                    continue
                health[name] = {
                    "health": rule.get("health", "unknown"),
                    "state": rule.get("state", "unknown"),
                    "last_error": rule.get("lastError"),
                }
        return health


def _extract_error(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict):
        return data.get("error") or data.get("errorType") or response.text
    return response.text
