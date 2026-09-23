"""API-level tests for GET/POST .../rules/export and .../rules/import: RBAC
(export=member, import=owner), route-ordering vs. GET .../rules/{slug}, the
version-400 mapping, and dry_run's zero-k8s-write guarantee.

Same mocking seams as test_rules_api.py: K8sClientFactory._co_api is
monkeypatched per test, Prometheus is respx-mocked against the default
cluster's configured prometheus_url.
"""

from typing import Any
from unittest.mock import MagicMock

import httpx
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

VALID_QUERY_RESPONSE = httpx.Response(200, json={"status": "success", "data": "vector(1)"})

EXPORT_ENVELOPE = {
    "kam_export_version": 1,
    "kind": "rules",
    "exported_at": "2026-01-01T00:00:00+00:00",
    "source": {"team_slug": "platform", "cluster_name": "local", "app_version": "dev"},
    "rules": [
        {
            "slug": "imported-rule",
            "alert_name": "ImportedRule",
            "expr": "vector(1)",
            "for": None,
            "severity": "info",
            "labels": {},
            "annotations": {},
            "runbook_url": None,
            "grafana_url": None,
            "mode": "promql",
            "builder_state": None,
        }
    ],
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


def _patch_co_api(app: FastAPI, fake_api: MagicMock) -> None:
    app.state.k8s_factory._co_api = lambda cluster: fake_api


async def _member_client(
    client: AsyncClient, *, role: str = "member", username: str = "alice"
) -> tuple[int, int]:
    team_id = await _create_team()
    cluster_id = await _default_cluster_id()
    await login_as(client, username=username)
    await _add_membership(client, team_id, role=role)
    return team_id, cluster_id


# -- export: RBAC + route ordering ---------------------------------------


async def test_export_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.get(f"/api/v1/teams/1/rules/export?cluster_id={cluster_id}")
    assert response.status_code == 401


async def test_export_non_member_is_403(client: AsyncClient) -> None:
    team_id = await _create_team()
    cluster_id = await _default_cluster_id()
    await login_as(client, username="carol")
    response = await client.get(f"/api/v1/teams/{team_id}/rules/export?cluster_id={cluster_id}")
    assert response.status_code == 403


async def test_export_member_can_export(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {"items": []}
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client, role="member")
    response = await client.get(f"/api/v1/teams/{team_id}/rules/export?cluster_id={cluster_id}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["kam_export_version"] == 1
    assert body["kind"] == "rules"
    assert body["rules"] == []


async def test_export_route_does_not_collide_with_get_slug(
    client: AsyncClient, app: FastAPI
) -> None:
    """"export" itself matches the {slug} pattern -- this is a regression
    guard that GET .../rules/export is routed to the export handler, not
    swallowed by GET .../rules/{slug}."""
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {"items": []}
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(f"/api/v1/teams/{team_id}/rules/export?cluster_id={cluster_id}")

    assert response.status_code == 200
    assert response.json()["kind"] == "rules"
    fake_api.get_namespaced_custom_object.assert_not_called()


async def test_export_filters_by_slugs(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {
        "items": [
            {
                "metadata": {
                    "name": "kam-t1-a",
                    "labels": {"app.kubernetes.io/managed-by": "kam", "kam/team-id": "1"},
                },
                "spec": {
                    "groups": [
                        {"rules": [{"alert": "A", "expr": "up", "labels": {"severity": "info"}}]}
                    ]
                },
            },
            {
                "metadata": {
                    "name": "kam-t1-b",
                    "labels": {"app.kubernetes.io/managed-by": "kam", "kam/team-id": "1"},
                },
                "spec": {
                    "groups": [
                        {"rules": [{"alert": "B", "expr": "up", "labels": {"severity": "info"}}]}
                    ]
                },
            },
        ]
    }
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    response = await client.get(
        f"/api/v1/teams/{team_id}/rules/export?cluster_id={cluster_id}&slugs=a"
    )

    body = response.json()
    assert [r["slug"] for r in body["rules"]] == ["a"]


async def test_export_writes_audit_row(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {"items": []}
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client)
    await client.get(f"/api/v1/teams/{team_id}/rules/export?cluster_id={cluster_id}")

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "rules.export"))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].team_id == team_id


# -- import: RBAC ---------------------------------------------------------


async def test_import_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.post(
        "/api/v1/teams/1/rules/import",
        json={
            "data": EXPORT_ENVELOPE,
            "target_cluster_id": cluster_id,
            "conflict_strategy": "skip",
            "dry_run": True,
        },
    )
    assert response.status_code == 401


async def test_import_member_non_owner_is_403(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client, role="member")
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules/import",
        json={
            "data": EXPORT_ENVELOPE,
            "target_cluster_id": cluster_id,
            "conflict_strategy": "skip",
            "dry_run": True,
        },
    )
    assert response.status_code == 403
    fake_api.create_namespaced_custom_object.assert_not_called()


async def test_import_non_member_of_team_is_403(client: AsyncClient) -> None:
    team_id = await _create_team()
    cluster_id = await _default_cluster_id()
    await login_as(client, username="carol")
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules/import",
        json={
            "data": EXPORT_ENVELOPE,
            "target_cluster_id": cluster_id,
            "conflict_strategy": "skip",
            "dry_run": True,
        },
    )
    assert response.status_code == 403


@respx.mock
async def test_import_owner_dry_run_makes_zero_k8s_writes(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client, role="owner")
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules/import",
        json={
            "data": EXPORT_ENVELOPE,
            "target_cluster_id": cluster_id,
            "conflict_strategy": "skip",
            "dry_run": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"] == {
        "created": 1,
        "skipped": 0,
        "overwritten": 0,
        "renamed": 0,
        "failed": 0,
    }
    assert body["verdicts"][0]["slug"] == "imported-rule"
    fake_api.create_namespaced_custom_object.assert_not_called()
    fake_api.replace_namespaced_custom_object.assert_not_called()


@respx.mock
async def test_import_owner_real_run_creates_and_audits(
    client: AsyncClient, app: FastAPI
) -> None:
    respx.post(FORMAT_QUERY_URL).mock(return_value=VALID_QUERY_RESPONSE)
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    fake_api.create_namespaced_custom_object.side_effect = lambda **kwargs: kwargs["body"]
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client, role="owner")
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules/import",
        json={
            "data": EXPORT_ENVELOPE,
            "target_cluster_id": cluster_id,
            "conflict_strategy": "skip",
            "dry_run": False,
        },
    )

    assert response.status_code == 200
    fake_api.create_namespaced_custom_object.assert_called_once()
    _, kwargs = fake_api.create_namespaced_custom_object.call_args
    assert kwargs["body"]["metadata"]["name"] == f"kam-t{team_id}-imported-rule"

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "rules.import"))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["dry_run"] is False
    assert rows[0].detail["summary"]["created"] == 1


async def test_import_unsupported_version_is_400(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    _patch_co_api(app, fake_api)

    team_id, cluster_id = await _member_client(client, role="owner")
    bad_envelope: dict[str, Any] = {**EXPORT_ENVELOPE, "kam_export_version": 2}
    response = await client.post(
        f"/api/v1/teams/{team_id}/rules/import",
        json={
            "data": bad_envelope,
            "target_cluster_id": cluster_id,
            "conflict_strategy": "skip",
            "dry_run": True,
        },
    )
    assert response.status_code == 400
    fake_api.create_namespaced_custom_object.assert_not_called()


async def test_import_prometheus_down_is_503(client: AsyncClient, app: FastAPI) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    _patch_co_api(app, fake_api)

    with respx.mock:
        respx.post(FORMAT_QUERY_URL).mock(side_effect=httpx.ConnectError("refused"))
        team_id, cluster_id = await _member_client(client, role="owner")
        response = await client.post(
            f"/api/v1/teams/{team_id}/rules/import",
            json={
                "data": EXPORT_ENVELOPE,
                "target_cluster_id": cluster_id,
                "conflict_strategy": "skip",
                "dry_run": True,
            },
        )
    assert response.status_code == 503
