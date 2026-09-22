"""Alertmanager API client: fetches live alerts from a cluster's Alertmanager.

Phase 3 only reads `/api/v2/alerts`. Silences (Phase 6) and any write
endpoints are out of scope here.
"""

from typing import Any

import httpx

from app.models.cluster import Cluster

DEFAULT_TIMEOUT = httpx.Timeout(5.0)


class AlertmanagerUnavailableError(Exception):
    """Raised when a cluster's Alertmanager can't be reached in time."""


class AlertmanagerClient:
    """Per-cluster async client for Alertmanager's v2 API.

    Takes a shared `httpx.AsyncClient` (created once in the app lifespan) so
    callers don't pay connection setup cost per cluster per request.
    """

    def __init__(self, cluster: Cluster, http_client: httpx.AsyncClient) -> None:
        self._cluster = cluster
        self._http = http_client

    async def get_alerts(self) -> list[dict[str, Any]]:
        url = f"{self._cluster.alertmanager_url}/api/v2/alerts"
        try:
            response = await self._http.get(
                url,
                params={"active": "true", "silenced": "true", "inhibited": "true"},
                timeout=DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.TransportError as exc:
            raise AlertmanagerUnavailableError(
                f"cannot reach alertmanager for cluster '{self._cluster.name}': {exc}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise AlertmanagerUnavailableError(
                f"alertmanager for cluster '{self._cluster.name}' returned "
                f"{exc.response.status_code}"
            ) from exc

        return response.json()
