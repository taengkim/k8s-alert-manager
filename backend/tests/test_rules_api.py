"""API-level tests for the rules endpoints: RBAC, expr-validation-before-k8s
ordering, conflict/ownership/unavailable error mapping, and best-effort
health merge from Prometheus.

The kubernetes CustomObjectsApi is mocked at the K8sClientFactory._co_api
seam (a real factory instance lives on app.state.k8s_factory once the app's
lifespan has started; we monkeypatch its _co_api method per test). Prometheus
calls go through the app's real shared httpx.AsyncClient, intercepted with
respx against the default cluster's configured prometheus_url.
"""

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi import FastAPI
from httpx import AsyncClient
from kubernetes.client.exceptions import ApiException
from sqlalchemy import select

import app.db as db_module
from app.config import get_settings
from app.models.audit import AuditLog
from app.models.cluster import Cluster
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

PROM_URL = "http://localhost:30090"
FORMAT_QUERY_URL = f"{PROM_URL}/api/v1/format_query"
RULES_HEALTH_URL = f"{PROM_URL}/api/v1/rules"

VALID_QUERY_RESPONSE = httpx.Response(200, json={"status": "success", "data": "vector(1)"})
INVALID_QUERY_RESPONSE = httpx.Response(
    400, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
)
EMPTY_HEALTH_RESPONSE = httpx.Response(
    200, json={"status": "success", "data": {"groups": []}}
)

RULE_BODY = {
    "slug": "e2e-test",
    "alert_name": "KamE2ETest",
    "expr": "vector(1)",
    "severity": "info",
}


async def _create_team(slug: str = "platform") -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(client: AsyncClient, team_id: int, role: str = "member") -> None:
    me = (await client.get("/api/v1/auth/me")).json()
    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(team_id=team_id, user_id=me["id"], role=role, origin="manual")
        )
        await session.commit()


async def _default_cluster_id() -> int:
    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
            )
        ).scalar_one()
        return cluster.id


def _owned_rule(name: str, team_id: str, alert_name: str = "X", expr: str = "up") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": "kam-rules",
            "resourceVersion": "1",
            "labels": {"app.kubernetes.io/managed-by": "kam", "kam/team-id": team_id},
        },
        "spec": {
            "groups": [
                {
                    "name": "kam-group",
                    "rules": [
                        {
                            "alert": alert_name,
                            "expr": expr,
                            "labels": {"kam_team": "platform", "severity": "info"},
                        }
                    ],
                }
            ]
        },
    }


def _patch_co_api(app: FastAPI, fake_api: MagicMock) -> None:
    app.state.k8s_factory._co_api = lambda cluster: fake_api


async def _member_client(client: AsyncClient, username: str = "alice") -> tuple[int, int]:
    """Log `client` in as `username`, create+join a fresh team, and return
    (team_id, default_cluster_id)."""
    team_id = await _create_team()
    cluster_id = await _default_cluster_id()
    await login_as(client, username=username)
    await _add_membership(client, team_id)
    return team_id, cluster_id


# -- RBAC ---------------------------------------------------------------


async def test_list_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.get(f"/api/v1/teams/1/rules?cluster_id={cluster_id}")
    assert response.status_code == 401


async def test_non_member_cannot_list(client: AsyncClient) -> None:
    team_id = await _create_team()
    cluster_id = await _default_cluster_id()
    await login_as(client, username="carol")
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")
    assert response.status_code == 403


async def test_validate_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.post(
        "/api/v1/rules/validate", json={"cluster_id": cluster_id, "expr": "up"}
    )
    assert response.status_code == 401


@respx.mock
async def test_validate_endpoint_returns_prometheus_result(client: AsyncClient) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    cluster_id = await _default_cluster_id()
    await login_as(client, username="alice")

    response = await client.post(
        "/api/v1/rules/validate", json={"cluster_id": cluster_id, "expr": "up"}
    )
    assert response.status_code == 200
    assert response.json() == {"valid": True, "error": None}


# -- create ---------------------------------------------------------------


@respx.mock
async def test_create_invalid_expr_is_422_and_never_calls_k8s(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=INVALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}",
        json={**RULE_BODY, "expr": "bad(("},
    )

    assert response.status_code == 422
    fake_api.create_namespaced_custom_object.assert_not_called()


@respx.mock
async def test_create_success_returns_parsed_rule_and_writes_audit_row(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.create_namespaced_custom_object.side_effect = lambda **kwargs: kwargs["body"]
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}", json=RULE_BODY
    )

    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "e2e-test"
    assert body["alert_name"] == "KamE2ETest"

    expected_name = f"kam-t{team_id}-e2e-test"
    _, kwargs = fake_api.create_namespaced_custom_object.call_args
    assert kwargs["body"]["metadata"]["name"] == expected_name
    assert kwargs["body"]["metadata"]["labels"]["kam/team-id"] == str(team_id)

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "rule.create"))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].object_ref == expected_name
    assert rows[0].team_id == team_id


@respx.mock
async def test_create_conflict_maps_to_409(client: AsyncClient, app: FastAPI) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.create_namespaced_custom_object.side_effect = ApiException(status=409)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}", json=RULE_BODY
    )
    assert response.status_code == 409


async def test_non_member_cannot_create(client: AsyncClient) -> None:
    team_id = await _create_team()
    cluster_id = await _default_cluster_id()
    await login_as(client, username="carol")
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}", json=RULE_BODY
    )
    assert response.status_code == 403


# -- list -------------------------------------------------------------


@respx.mock
async def test_list_scopes_by_managed_by_and_team_id_label_selector(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.get(RULES_HEALTH_URL).mock(return_value=EMPTY_HEALTH_RESPONSE)
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {
        "items": [_owned_rule("kam-platform-x", "1")]
    }
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")

    assert response.status_code == 200
    _, kwargs = fake_api.list_namespaced_custom_object.call_args
    assert kwargs["label_selector"] == f"app.kubernetes.io/managed-by=kam,kam/team-id={team_id}"


@respx.mock
async def test_list_merges_health_from_prometheus(client: AsyncClient, app: FastAPI) -> None:
    respx.get(RULES_HEALTH_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "groups": [
                        {
                            "rules": [
                                {
                                    "name": "KamAlwaysFiring",
                                    "health": "ok",
                                    "state": "firing",
                                    "lastError": None,
                                }
                            ]
                        }
                    ]
                },
            },
        )
    )
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {
        "items": [_owned_rule("kam-platform-always-firing", "1", alert_name="KamAlwaysFiring")]
    }
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")

    body = response.json()
    assert body["rules"][0]["health"] == "ok"
    assert body["rules"][0]["state"] == "firing"
    assert "warning" not in body


@respx.mock
async def test_list_prometheus_down_still_200_with_unknown_health(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.get(RULES_HEALTH_URL).mock(side_effect=httpx.ConnectError("refused"))
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {
        "items": [_owned_rule("kam-platform-x", "1")]
    }
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["rules"][0]["health"] == "unknown"
    assert "warning" in body


async def test_list_k8s_unavailable_returns_503(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.side_effect = ApiException(status=500)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")
    assert response.status_code == 503


# -- get single -------------------------------------------------------------


async def test_get_returns_404_when_absent(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules/nope?cluster_id={cluster_id}")
    assert response.status_code == 404


async def test_get_returns_404_when_owned_by_a_different_team(
    client: AsyncClient, app: FastAPI
) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-other-x", "999")
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}")
    assert response.status_code == 404


# -- put / delete ownership guard ---------------------------------------


@respx.mock
async def test_put_on_foreign_team_rule_is_403(client: AsyncClient, app: FastAPI) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-platform-x", "999")
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.put(
        f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}", json=RULE_BODY
    )

    assert response.status_code == 403
    fake_api.replace_namespaced_custom_object.assert_not_called()


@respx.mock
async def test_put_on_rule_missing_managed_by_label_is_403(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = {
        "metadata": {"name": "kam-platform-x", "labels": {}},
        "spec": {"groups": []},
    }
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.put(
        f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}", json=RULE_BODY
    )

    assert response.status_code == 403
    fake_api.replace_namespaced_custom_object.assert_not_called()


@respx.mock
async def test_put_success_audits_rule_update(client: AsyncClient, app: FastAPI) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-platform-x", "1")
    fake_api.replace_namespaced_custom_object.side_effect = lambda **kwargs: kwargs["body"]
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.put(
        f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}",
        json={**RULE_BODY, "severity": "warning"},
    )

    assert response.status_code == 200
    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "rule.update"))
        ).scalars().all()
    assert len(rows) == 1


async def test_delete_on_foreign_team_rule_is_403(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-platform-x", "999")
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.delete(f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}")

    assert response.status_code == 403
    fake_api.delete_namespaced_custom_object.assert_not_called()


async def test_delete_success_audits_rule_delete(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-platform-x", "1")
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.delete(f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}")

    assert response.status_code == 204
    fake_api.delete_namespaced_custom_object.assert_called_once()
    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "rule.delete"))
        ).scalars().all()
    assert len(rows) == 1


async def test_delete_returns_404_when_absent(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.delete(f"/api/v1/teams/{team_id}/rules/ghost?cluster_id={cluster_id}")
    assert response.status_code == 404


# -- error mapping: bad request vs. unavailable vs. update conflict -----


async def test_list_maps_other_4xx_to_422(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.side_effect = ApiException(status=400)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")
    assert response.status_code == 422


@pytest.mark.parametrize("status_code", [401, 403, 429])
async def test_list_maps_auth_and_rate_limit_statuses_to_503_not_422(
    status_code: int, client: AsyncClient, app: FastAPI
) -> None:
    """401/403/429 are "the cluster is unreachable/rejecting us", not "our
    request was malformed" -- they must not become a 422."""
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.side_effect = ApiException(status=status_code)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")
    assert response.status_code == 503


async def test_list_error_detail_never_leaks_raw_response_headers(
    client: AsyncClient, app: FastAPI
) -> None:
    exc = ApiException(status=500, reason="Internal Server Error")
    exc.body = '{"message": "etcd unavailable"}'
    exc.headers = {"Audit-Id": "should-not-appear"}
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.side_effect = exc
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "etcd unavailable" in detail
    assert "should-not-appear" not in detail
    assert "HTTP response headers" not in detail


@respx.mock
async def test_put_resource_version_conflict_maps_to_409_korean_detail(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-platform-x", "1")
    fake_api.replace_namespaced_custom_object.side_effect = ApiException(status=409)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.put(
        f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}", json=RULE_BODY
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "동시 수정 충돌"


async def test_delete_resource_version_conflict_maps_to_409_korean_detail(
    client: AsyncClient, app: FastAPI
) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-platform-x", "1")
    fake_api.delete_namespaced_custom_object.side_effect = ApiException(status=409)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.delete(f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}")

    assert response.status_code == 409
    assert response.json()["detail"] == "동시 수정 충돌"


@respx.mock
async def test_put_other_4xx_maps_to_422_with_k8s_message(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule("kam-platform-x", "1")
    fake_api.replace_namespaced_custom_object.side_effect = ApiException(status=400)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.put(
        f"/api/v1/teams/{team_id}/rules/x?cluster_id={cluster_id}", json=RULE_BODY
    )
    assert response.status_code == 422


async def test_create_other_4xx_maps_to_422(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.create_namespaced_custom_object.side_effect = ApiException(status=400)
    _patch_co_api(app, fake_api)

    with respx.mock:
        respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
        team_id, cluster_id = await _member_client(client)
        response = await client.post(
            f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}", json=RULE_BODY
        )
    assert response.status_code == 422


# -- slug pattern validation ----------------------------------------------


async def test_create_rejects_slug_with_trailing_hyphen(
    client: AsyncClient, app: FastAPI
) -> None:
    fake_api = MagicMock()
    _patch_co_api(app, fake_api)

    with respx.mock:
        respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
        team_id, cluster_id = await _member_client(client)
        response = await client.post(
            f"/api/v1/teams/{team_id}/rules?cluster_id={cluster_id}",
            json={**RULE_BODY, "slug": "trailing-hyphen-"},
        )

    assert response.status_code == 422
    fake_api.create_namespaced_custom_object.assert_not_called()


async def test_get_rejects_invalid_slug_in_path(client: AsyncClient) -> None:
    team_id, cluster_id = await _member_client(client)
    response = await client.get(
        f"/api/v1/teams/{team_id}/rules/Not_Valid?cluster_id={cluster_id}"
    )
    assert response.status_code == 422
