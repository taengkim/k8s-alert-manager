"""Tests for `app.services.cluster_health`: per-component ok/fail
independence, the 30s cache (including that a cached hit makes no upstream
call at all), `refresh=true` bypassing it, and that error strings never leak
credential material -- plus the `GET /clusters/{id}/health` API's RBAC.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpx
import respx
from httpx import AsyncClient
from kubernetes.client.exceptions import ApiException

from app.models.cluster import Cluster
from app.services.cluster_health import ClusterHealthCache, get_health
from app.services.k8s import K8sClientFactory
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"
PROM_BUILDINFO = "http://localhost:30090/api/v1/status/buildinfo"
AM_STATUS = "http://localhost:30093/api/v2/status"


def _cluster(**overrides) -> Cluster:
    defaults: dict = {
        "id": 1,
        "name": "test-cluster",
        "display_name": "test-cluster",
        "k8s_auth_kind": "kubeconfig",
        "prometheus_url": "http://localhost:30090",
        "alertmanager_url": "http://localhost:30093",
        "rules_namespace": "kam-rules",
        "webhook_token_hash": "x",
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return Cluster(**defaults)


def _factory_with_fake_version_api(fake_api: MagicMock) -> K8sClientFactory:
    factory = K8sClientFactory()
    factory._version_api = lambda cluster: fake_api  # type: ignore[method-assign]
    return factory


# -- get_health: per-component independence ----------------------------------


@respx.mock
async def test_components_are_independently_ok_or_failing() -> None:
    respx.get(PROM_BUILDINFO).mock(return_value=httpx.Response(200, json={"status": "success"}))
    respx.get(AM_STATUS).mock(side_effect=httpx.ConnectError("refused"))

    fake_version_api = MagicMock()
    fake_version_api.get_code.return_value = MagicMock()
    factory = _factory_with_fake_version_api(fake_version_api)

    async with httpx.AsyncClient() as http_client:
        result = await get_health(_cluster(), k8s_factory=factory, http_client=http_client)

    assert result["k8s"]["ok"] is True
    assert result["prometheus"]["ok"] is True
    assert result["alertmanager"]["ok"] is False
    assert "error" in result["alertmanager"]


@respx.mock
async def test_k8s_api_exception_is_reported_safely() -> None:
    respx.get(PROM_BUILDINFO).mock(return_value=httpx.Response(200, json={}))
    respx.get(AM_STATUS).mock(return_value=httpx.Response(200, json={}))

    fake_version_api = MagicMock()
    fake_version_api.get_code.side_effect = ApiException(status=401, reason="Unauthorized")
    factory = _factory_with_fake_version_api(fake_version_api)

    async with httpx.AsyncClient() as http_client:
        result = await get_health(_cluster(), k8s_factory=factory, http_client=http_client)

    assert result["k8s"]["ok"] is False
    assert "Unauthorized" in result["k8s"]["error"]


async def test_k8s_credential_failure_does_not_leak_secret() -> None:
    """A cluster with corrupted/tampered credentials -- K8sClientFactory
    itself refuses to build a client and raises K8sUnavailableError with an
    already-safe message (see test_k8s_client_factory.py); the health
    service must surface that as-is, never falling back to str() on
    whatever raised inside client construction.
    """
    secret_fragment = "not-a-real-fernet-token-zzz"
    cluster = _cluster(credentials_encrypted=secret_fragment)
    factory = K8sClientFactory()

    async with httpx.AsyncClient() as http_client:
        result = await get_health(cluster, k8s_factory=factory, http_client=http_client)

    assert result["k8s"]["ok"] is False
    assert secret_fragment not in result["k8s"]["error"]
    assert "credentials/config invalid" in result["k8s"]["error"]


# -- ClusterHealthCache: 30s cache + refresh ---------------------------------


@respx.mock
async def test_second_call_within_ttl_does_not_hit_upstream_again() -> None:
    prom_route = respx.get(PROM_BUILDINFO).mock(return_value=httpx.Response(200, json={}))
    am_route = respx.get(AM_STATUS).mock(return_value=httpx.Response(200, json={}))

    fake_version_api = MagicMock()
    fake_version_api.get_code.return_value = MagicMock()
    factory = _factory_with_fake_version_api(fake_version_api)
    cache = ClusterHealthCache()

    async with httpx.AsyncClient() as http_client:
        first = await cache.get(_cluster(), k8s_factory=factory, http_client=http_client)
        second = await cache.get(_cluster(), k8s_factory=factory, http_client=http_client)

    assert first == second
    assert prom_route.call_count == 1
    assert am_route.call_count == 1
    assert fake_version_api.get_code.call_count == 1


@respx.mock
async def test_refresh_true_bypasses_cache() -> None:
    prom_route = respx.get(PROM_BUILDINFO).mock(return_value=httpx.Response(200, json={}))
    respx.get(AM_STATUS).mock(return_value=httpx.Response(200, json={}))

    fake_version_api = MagicMock()
    fake_version_api.get_code.return_value = MagicMock()
    factory = _factory_with_fake_version_api(fake_version_api)
    cache = ClusterHealthCache()

    async with httpx.AsyncClient() as http_client:
        await cache.get(_cluster(), k8s_factory=factory, http_client=http_client)
        await cache.get(_cluster(), k8s_factory=factory, http_client=http_client, refresh=True)

    assert prom_route.call_count == 2


def test_peek_returns_none_before_any_fetch() -> None:
    cache = ClusterHealthCache()
    assert cache.peek(999) is None


@respx.mock
async def test_peek_returns_cached_result_after_fetch() -> None:
    respx.get(PROM_BUILDINFO).mock(return_value=httpx.Response(200, json={}))
    respx.get(AM_STATUS).mock(return_value=httpx.Response(200, json={}))

    fake_version_api = MagicMock()
    fake_version_api.get_code.return_value = MagicMock()
    factory = _factory_with_fake_version_api(fake_version_api)
    cache = ClusterHealthCache()
    cluster = _cluster()

    async with httpx.AsyncClient() as http_client:
        fetched = await cache.get(cluster, k8s_factory=factory, http_client=http_client)

    assert cache.peek(cluster.id) == fetched


# -- GET /clusters/{id}/health API -------------------------------------------


async def _staging_cluster_id(client: AsyncClient) -> int:
    response = await client.post(
        "/api/v1/clusters",
        json={
            "name": "staging-health",
            "display_name": "Staging Health",
            "prometheus_url": "http://localhost:30090",
            "alertmanager_url": "http://localhost:30093",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


async def test_health_endpoint_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/clusters/1/health")
    assert response.status_code == 401


async def test_health_endpoint_forbidden_for_non_admin_no_heartbeat_team(
    client: AsyncClient,
) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    cluster_id = await _staging_cluster_id(client)

    await login_as(client, username="bob")
    response = await client.get(f"/api/v1/clusters/{cluster_id}/health")
    assert response.status_code == 403


@respx.mock
async def test_health_endpoint_returns_three_components_for_admin(client: AsyncClient, app) -> None:
    respx.get(PROM_BUILDINFO).mock(return_value=httpx.Response(200, json={}))
    respx.get(AM_STATUS).mock(return_value=httpx.Response(200, json={}))

    # Avoid touching a real k8s API server: patch the app's own shared
    # K8sClientFactory's version-api seam, the same seam
    # test_k8s_client_factory.py/test_cluster_health's unit tests use.
    fake_version_api = MagicMock()
    fake_version_api.get_code.return_value = MagicMock()
    app.state.k8s_factory._version_api = lambda cluster: fake_version_api

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    cluster_id = await _staging_cluster_id(client)

    response = await client.get(f"/api/v1/clusters/{cluster_id}/health")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"k8s", "prometheus", "alertmanager"}
    assert body["k8s"]["ok"] is True
    assert body["prometheus"]["ok"] is True
    assert body["alertmanager"]["ok"] is True
