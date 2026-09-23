"""API-level tests for the metrics-browser endpoints: guardrails (names
cache TTL/search/limit, query_range step computation/range cap/series
truncation), Prometheus error mapping, and auth/cluster-id checks.

Prometheus calls go through the app's real shared httpx.AsyncClient,
intercepted with respx against the default cluster's configured
prometheus_url -- same pattern as tests/test_rules_api.py.
"""

import httpx
import respx
from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.api import metrics as metrics_api
from app.config import get_settings
from app.models.cluster import Cluster
from tests.conftest import login_as

PROM_URL = "http://localhost:30090"
NAMES_URL = f"{PROM_URL}/api/v1/label/__name__/values"
METADATA_URL = f"{PROM_URL}/api/v1/metadata"
LABELS_URL = f"{PROM_URL}/api/v1/labels"
LABEL_VALUES_URL = f"{PROM_URL}/api/v1/label/job/values"
QUERY_URL = f"{PROM_URL}/api/v1/query"
QUERY_RANGE_URL = f"{PROM_URL}/api/v1/query_range"

NAMES_RESPONSE = httpx.Response(
    200, json={"status": "success", "data": ["node_load1", "node_load15", "up"]}
)


async def _default_cluster_id() -> int:
    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
            )
        ).scalar_one()
        return cluster.id


async def _login(client: AsyncClient, username: str = "alice") -> None:
    await login_as(client, username=username)


def _reset_names_cache() -> None:
    metrics_api._names_cache.clear()


# -- auth / cluster-id -----------------------------------------------------


async def test_names_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.get(f"/api/v1/metrics/names?cluster_id={cluster_id}")
    assert response.status_code == 401


async def test_metadata_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.get(
        f"/api/v1/metrics/metadata?cluster_id={cluster_id}&metric=up"
    )
    assert response.status_code == 401


async def test_labels_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.get(f"/api/v1/metrics/labels?cluster_id={cluster_id}&metric=up")
    assert response.status_code == 401


async def test_label_values_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.get(
        f"/api/v1/metrics/label-values?cluster_id={cluster_id}&metric=up&label=job"
    )
    assert response.status_code == 401


async def test_query_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.post(
        "/api/v1/metrics/query", json={"cluster_id": cluster_id, "query": "up"}
    )
    assert response.status_code == 401


async def test_query_range_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": 3600},
    )
    assert response.status_code == 401


@respx.mock
async def test_bad_cluster_id_is_404(client: AsyncClient) -> None:
    await _login(client)
    response = await client.get("/api/v1/metrics/names?cluster_id=999999")
    assert response.status_code == 404


# -- names: cache + search + limit -----------------------------------------


@respx.mock
async def test_names_search_filter_and_limit_applied(client: AsyncClient) -> None:
    _reset_names_cache()
    route = respx.get(NAMES_URL).mock(return_value=NAMES_RESPONSE)
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.get(
        f"/api/v1/metrics/names?cluster_id={cluster_id}&search=load&limit=1"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["names"] == ["node_load1"]
    assert route.call_count == 1


@respx.mock
async def test_names_cache_ttl_respected_within_window(client: AsyncClient) -> None:
    _reset_names_cache()
    route = respx.get(NAMES_URL).mock(return_value=NAMES_RESPONSE)
    cluster_id = await _default_cluster_id()
    await _login(client)

    first = await client.get(f"/api/v1/metrics/names?cluster_id={cluster_id}")
    second = await client.get(f"/api/v1/metrics/names?cluster_id={cluster_id}")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    # The second call must be served from the in-process cache -- Prometheus
    # is only hit once within the 60s TTL window.
    assert route.call_count == 1


@respx.mock
async def test_names_upstream_unavailable_is_503(client: AsyncClient) -> None:
    _reset_names_cache()
    respx.get(NAMES_URL).mock(side_effect=httpx.ConnectError("refused"))
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.get(f"/api/v1/metrics/names?cluster_id={cluster_id}")
    assert response.status_code == 503


# -- metadata / labels / label-values ---------------------------------------


@respx.mock
async def test_metadata_returns_first_entry(client: AsyncClient) -> None:
    respx.get(METADATA_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"up": [{"type": "gauge", "help": "up help", "unit": ""}]},
            },
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.get(
        f"/api/v1/metrics/metadata?cluster_id={cluster_id}&metric=up"
    )
    assert response.status_code == 200
    assert response.json() == {"type": "gauge", "help": "up help", "unit": ""}


@respx.mock
async def test_metadata_absent_metric_is_empty_object(client: AsyncClient) -> None:
    respx.get(METADATA_URL).mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {}})
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.get(
        f"/api/v1/metrics/metadata?cluster_id={cluster_id}&metric=nonexistent"
    )
    assert response.status_code == 200
    assert response.json() == {}


@respx.mock
async def test_labels_excludes_dunder_name(client: AsyncClient) -> None:
    respx.get(LABELS_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": ["__name__", "job", "instance"]}
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.get(f"/api/v1/metrics/labels?cluster_id={cluster_id}&metric=up")
    assert response.status_code == 200
    assert response.json() == {"labels": ["job", "instance"]}


@respx.mock
async def test_label_values_capped_at_200(client: AsyncClient) -> None:
    respx.get(LABEL_VALUES_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": [f"v{i}" for i in range(250)]}
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.get(
        f"/api/v1/metrics/label-values?cluster_id={cluster_id}&metric=up&label=job"
    )
    assert response.status_code == 200
    assert len(response.json()["values"]) == 200


async def test_label_values_rejects_malformed_label_name(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.get(
        f"/api/v1/metrics/label-values?cluster_id={cluster_id}&metric=up&label=../../etc"
    )
    assert response.status_code == 422


# -- query -------------------------------------------------------------


@respx.mock
async def test_query_returns_samples_and_series_count(client: AsyncClient) -> None:
    respx.get(QUERY_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": {"instance": "a"}, "value": [1700000000, "1.5"]},
                        {"metric": {"instance": "b"}, "value": [1700000000, "2.5"]},
                    ],
                },
            },
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.post(
        "/api/v1/metrics/query", json={"cluster_id": cluster_id, "query": "up > 0"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["series_count"] == 2
    assert body["samples"] == [
        {"labels": {"instance": "a"}, "value": 1.5},
        {"labels": {"instance": "b"}, "value": 2.5},
    ]


@respx.mock
async def test_query_upstream_400_is_422(client: AsyncClient) -> None:
    respx.get(QUERY_URL).mock(
        return_value=httpx.Response(
            400,
            json={"status": "error", "errorType": "bad_data", "error": "parse error: bad syntax"},
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.post(
        "/api/v1/metrics/query", json={"cluster_id": cluster_id, "query": "bad(("}
    )
    assert response.status_code == 422
    assert "parse error" in response.json()["detail"]


# -- query_range: step computation, range cap, truncation, errors ----------


@respx.mock
async def test_query_range_default_step_computed(client: AsyncClient) -> None:
    route = respx.get(QUERY_RANGE_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    # 1 hour range -> range_seconds/250 = 14.4, floored up to the 15s minimum.
    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": 3600},
    )
    assert response.status_code == 200
    assert response.json()["step_used"] == 15.0
    sent_params = dict(route.calls[0].request.url.params)
    assert sent_params["step"] == "15.0"


@respx.mock
async def test_query_range_default_step_scales_with_range(client: AsyncClient) -> None:
    respx.get(QUERY_RANGE_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    # 24h range -> 86400/250 = 345.6s, well above the 15s floor.
    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": 86400},
    )
    assert response.status_code == 200
    assert response.json()["step_used"] == 86400 / 250


@respx.mock
async def test_query_range_explicit_tiny_step_floored_to_500_points(
    client: AsyncClient,
) -> None:
    respx.get(QUERY_RANGE_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": []}}
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    # 1h range with a 1s step would be 3600 points; must be floored to
    # exactly 500 points (step = range/500 = 7.2s).
    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": 3600, "step": 1},
    )
    assert response.status_code == 200
    assert response.json()["step_used"] == 3600 / 500


@respx.mock
async def test_query_range_over_7_days_is_422(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _login(client)

    eight_days = 8 * 24 * 3600
    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": eight_days},
    )
    assert response.status_code == 422


@respx.mock
async def test_query_range_51_series_truncated_to_50(client: AsyncClient) -> None:
    matrix = [
        {"metric": {"instance": str(i)}, "values": [[0, "1"]]} for i in range(51)
    ]
    respx.get(QUERY_RANGE_URL).mock(
        return_value=httpx.Response(
            200, json={"status": "success", "data": {"resultType": "matrix", "result": matrix}}
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": 3600},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["truncated"] is True
    assert len(body["series"]) == 50
    assert body["total_series"] == 51


@respx.mock
async def test_query_range_upstream_400_is_422_with_error(client: AsyncClient) -> None:
    respx.get(QUERY_RANGE_URL).mock(
        return_value=httpx.Response(
            400,
            json={"status": "error", "errorType": "bad_data", "error": "parse error: bad syntax"},
        )
    )
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "bad((", "start": 0, "end": 3600},
    )
    assert response.status_code == 422
    assert "parse error" in response.json()["detail"]


@respx.mock
async def test_query_range_timeout_is_503(client: AsyncClient) -> None:
    respx.get(QUERY_RANGE_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": 3600},
    )
    assert response.status_code == 503


@respx.mock
async def test_query_range_connect_error_is_503(client: AsyncClient) -> None:
    respx.get(QUERY_RANGE_URL).mock(side_effect=httpx.ConnectError("refused"))
    cluster_id = await _default_cluster_id()
    await _login(client)

    response = await client.post(
        "/api/v1/metrics/query_range",
        json={"cluster_id": cluster_id, "query": "up", "start": 0, "end": 3600},
    )
    assert response.status_code == 503
