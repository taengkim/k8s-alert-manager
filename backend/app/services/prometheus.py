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


class PrometheusClient:
    def __init__(self, cluster: Cluster, http_client: httpx.AsyncClient) -> None:
        self._cluster = cluster
        self._http = http_client

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
