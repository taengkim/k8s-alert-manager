"""Alertmanager API client: fetches live alerts and manages silences for a
cluster's Alertmanager.
"""

from typing import Any

import httpx

from app.models.cluster import Cluster

DEFAULT_TIMEOUT = httpx.Timeout(5.0)


class AlertmanagerUnavailableError(Exception):
    """Raised when a cluster's Alertmanager can't be reached in time, or
    returns an unexpected (non-2xx, non-tolerated) status."""


class AlertmanagerBadRequestError(Exception):
    """Raised when Alertmanager rejects a silence create as malformed (400)
    -- the caller's own input, as opposed to the service being unreachable.
    The API layer maps this to 422, surfacing AM's own message."""


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

    async def get_silences(self) -> list[dict[str, Any]]:
        url = f"{self._cluster.alertmanager_url}/api/v2/silences"
        try:
            response = await self._http.get(url, timeout=DEFAULT_TIMEOUT)
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

    async def create_silence(self, payload: dict[str, Any]) -> str:
        """POST a new silence. `payload` is the full AM v2 silence body
        (matchers/startsAt/endsAt/createdBy/comment) -- built by the caller
        so this client stays a thin transport layer. Returns the new
        silence's AM-assigned id.
        """
        url = f"{self._cluster.alertmanager_url}/api/v2/silences"
        try:
            response = await self._http.post(url, json=payload, timeout=DEFAULT_TIMEOUT)
        except httpx.TransportError as exc:
            raise AlertmanagerUnavailableError(
                f"cannot reach alertmanager for cluster '{self._cluster.name}': {exc}"
            ) from exc

        if response.status_code == 400:
            raise AlertmanagerBadRequestError(_extract_message(response))

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AlertmanagerUnavailableError(
                f"alertmanager for cluster '{self._cluster.name}' returned "
                f"{exc.response.status_code}"
            ) from exc

        data = response.json()
        return data["silenceID"]

    async def expire_silence(self, silence_id: str) -> None:
        url = f"{self._cluster.alertmanager_url}/api/v2/silence/{silence_id}"
        try:
            response = await self._http.delete(url, timeout=DEFAULT_TIMEOUT)
        except httpx.TransportError as exc:
            raise AlertmanagerUnavailableError(
                f"cannot reach alertmanager for cluster '{self._cluster.name}': {exc}"
            ) from exc

        # A silence that's already gone (expired + GC'd, or never existed)
        # is not an error for an "expire" -- the desired end state
        # (not-silenced) already holds.
        if response.status_code == 404:
            return

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AlertmanagerUnavailableError(
                f"alertmanager for cluster '{self._cluster.name}' returned "
                f"{exc.response.status_code}"
            ) from exc


def _extract_message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict):
        return data.get("message") or response.text
    return response.text
