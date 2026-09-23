"""Per-cluster health probing for the k8s API server, Prometheus, and
Alertmanager -- backs the admin clusters page's three health dots and the
cheap health summary folded into `GET /clusters`.

`get_health()` is one uncached probe of all three components, run
concurrently. `ClusterHealthCache` wraps it with a 30s in-process cache, one
instance per app (see `app.main`'s lifespan and `app.api.deps.
get_cluster_health_cache`) -- deliberately an object on `app.state`, not a
module-level dict, so it resets between test app instances instead of
leaking cached results across the test suite (the same pattern
`K8sClientFactory`'s ApiClient cache uses).

Every error string here is a class name plus a short, deliberately generic
message -- never `str(exc)` -- reusing `app.services.k8s`'s safe-messaging
convention: a raw exception message could embed decrypted credentials (a
bad kubeconfig/token) or other cluster-internal detail that must never reach
an API response.
"""

import asyncio
import time
from typing import Any

import httpx
from kubernetes.client.exceptions import ApiException

from app.models.cluster import Cluster
from app.services.k8s import (
    K8sClientFactory,
    K8sUnavailableError,
    _api_exception_detail,
)

CACHE_TTL_SECONDS = 30.0
HTTP_TIMEOUT = httpx.Timeout(5.0)
K8S_TIMEOUT_SECONDS = 5.0


async def _check_k8s(cluster: Cluster, k8s_factory: K8sClientFactory) -> dict[str, Any]:
    start = time.monotonic()
    try:
        version_api = k8s_factory._version_api(cluster)
    except K8sUnavailableError as exc:
        # Already a safe, class-name-prefixed message (see
        # K8sClientFactory._build) -- pass it through as-is.
        return {"ok": False, "latency_ms": None, "error": f"K8sUnavailableError: {exc}"}

    try:
        await asyncio.wait_for(
            asyncio.to_thread(version_api.get_code), timeout=K8S_TIMEOUT_SECONDS
        )
    except ApiException as exc:
        return {
            "ok": False,
            "latency_ms": None,
            "error": f"ApiException: {_api_exception_detail(exc)}",
        }
    except TimeoutError:
        return {
            "ok": False,
            "latency_ms": None,
            "error": "TimeoutError: k8s API did not respond in time",
        }
    except Exception as exc:  # noqa: BLE001
        # Broad on purpose: connection/TLS errors from the underlying
        # urllib3 transport aren't a typed error here, and their messages
        # can carry request-internal detail we don't want to surface --
        # only the exception's class name is safe to expose.
        return {"ok": False, "latency_ms": None, "error": f"{type(exc).__name__}: unreachable"}

    return {"ok": True, "latency_ms": round((time.monotonic() - start) * 1000), "error": None}


async def _check_http(http_client: httpx.AsyncClient, url: str) -> dict[str, Any]:
    start = time.monotonic()
    try:
        response = await http_client.get(url, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
    except httpx.TransportError as exc:
        return {"ok": False, "latency_ms": None, "error": f"{type(exc).__name__}: unreachable"}
    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "latency_ms": None,
            "error": f"HTTPStatusError: {exc.response.status_code}",
        }
    return {"ok": True, "latency_ms": round((time.monotonic() - start) * 1000), "error": None}


async def get_health(
    cluster: Cluster,
    *,
    k8s_factory: K8sClientFactory,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    """One uncached probe of all three components, run concurrently.

    Callers wanting the 30s cache (the admin page's poll, `GET /clusters`'
    summary) should go through `ClusterHealthCache.get`/`.peek` instead of
    calling this directly on every request.
    """
    k8s_result, prometheus_result, alertmanager_result = await asyncio.gather(
        _check_k8s(cluster, k8s_factory),
        _check_http(
            http_client, f"{cluster.prometheus_url.rstrip('/')}/api/v1/status/buildinfo"
        ),
        _check_http(http_client, f"{cluster.alertmanager_url.rstrip('/')}/api/v2/status"),
    )
    return {
        "k8s": k8s_result,
        "prometheus": prometheus_result,
        "alertmanager": alertmanager_result,
    }


class ClusterHealthCache:
    """30s in-process cache of `get_health()` results, keyed by cluster id.

    One instance lives on `app.state.cluster_health_cache` (see
    `app.main.lifespan`), constructed fresh per app instance -- the test app
    fixture builds a new FastAPI app per test, so this never leaks a cached
    "cluster is down" result from one test into the next.
    """

    def __init__(self) -> None:
        self._entries: dict[int, tuple[float, dict[str, Any]]] = {}

    async def get(
        self,
        cluster: Cluster,
        *,
        k8s_factory: K8sClientFactory,
        http_client: httpx.AsyncClient,
        refresh: bool = False,
    ) -> dict[str, Any]:
        now = time.monotonic()
        if not refresh:
            cached = self._entries.get(cluster.id)
            if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
                return cached[1]

        result = await get_health(cluster, k8s_factory=k8s_factory, http_client=http_client)
        self._entries[cluster.id] = (now, result)
        return result

    def peek(self, cluster_id: int) -> dict[str, Any] | None:
        """A non-fetching read of the cache -- returns `None` if nothing is
        cached yet or the entry has expired, never triggers a probe. Used by
        `GET /clusters`' health summary field, which must stay cheap (no
        live network calls) for a plain listing request.
        """
        cached = self._entries.get(cluster_id)
        if cached is None or time.monotonic() - cached[0] >= CACHE_TTL_SECONDS:
            return None
        return cached[1]
