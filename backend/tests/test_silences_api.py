"""API-level tests for Alertmanager silence management: list (+ audit
join/team scoping), create (AM call shape, audit row + team attribution,
error mapping, compensating expire on DB failure), and expire (permission
matrix, 404-tolerant).
"""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import respx
from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.config import get_settings
from app.models.cluster import Cluster
from app.models.silence import SilenceAudit
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

AM_BASE = "http://localhost:30093/api/v2"
SILENCES_URL = f"{AM_BASE}/silences"

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


def _raw_silence(silence_id: str, state: str = "active") -> dict:
    return {
        "id": silence_id,
        "matchers": [{"name": "alertname", "value": "KamAlwaysFiring", "isRegex": False, "isEqual": True}],
        "startsAt": "2026-09-23T00:00:00Z",
        "endsAt": "2026-09-23T01:00:00Z",
        "createdBy": "alice",
        "comment": "test silence",
        "status": {"state": state},
    }


async def _default_cluster_id() -> int:
    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
            )
        ).scalar_one()
        return cluster.id


async def _create_team(slug: str) -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(client: AsyncClient, team_id: int, role: str = "member") -> None:
    me = (await client.get("/api/v1/auth/me")).json()
    async with db_module.async_session_factory() as session:
        session.add(TeamMembership(team_id=team_id, user_id=me["id"], role=role, origin="manual"))
        await session.commit()


async def _insert_audit(
    *, cluster_id: int, am_silence_id: str, team_id: int | None, created_by: int | None = None
) -> None:
    async with db_module.async_session_factory() as session:
        session.add(
            SilenceAudit(
                am_silence_id=am_silence_id,
                cluster_id=cluster_id,
                team_id=team_id,
                created_by=created_by,
                matchers=[{"name": "alertname", "value": "X", "isRegex": False, "isEqual": True}],
                starts_at=datetime.now(UTC),
                ends_at=datetime.now(UTC) + timedelta(hours=1),
                comment="test",
            )
        )
        await session.commit()


# -- GET / list --------------------------------------------------------


async def test_list_requires_auth(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    response = await client.get(f"/api/v1/silences?cluster_id={cluster_id}")
    assert response.status_code == 401


@respx.mock
async def test_list_joins_audit_and_scopes_by_membership(client: AsyncClient) -> None:
    """A member sees their own team's silences plus unattributed ("external")
    ones, but not another team's."""
    respx.get(SILENCES_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                _raw_silence("sil-platform"),
                _raw_silence("sil-payments"),
                _raw_silence("sil-external"),
            ],
        )
    )
    cluster_id = await _default_cluster_id()
    platform_id = await _create_team("platform")
    payments_id = await _create_team("payments")
    await _insert_audit(cluster_id=cluster_id, am_silence_id="sil-platform", team_id=platform_id)
    await _insert_audit(cluster_id=cluster_id, am_silence_id="sil-payments", team_id=payments_id)
    # sil-external has no audit row at all.

    await login_as(client, username="alice")
    await _add_membership(client, platform_id)

    response = await client.get(f"/api/v1/silences?cluster_id={cluster_id}")
    assert response.status_code == 200
    body = response.json()
    by_id = {s["id"]: s for s in body["silences"]}
    assert set(by_id) == {"sil-platform", "sil-external"}
    assert by_id["sil-platform"]["team"] == {"id": platform_id, "slug": "platform"}
    assert by_id["sil-external"]["team"] is None


@respx.mock
async def test_admin_sees_all_silences_unscoped(client: AsyncClient) -> None:
    respx.get(SILENCES_URL).mock(
        return_value=httpx.Response(200, json=[_raw_silence("sil-a"), _raw_silence("sil-b")])
    )
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await _insert_audit(cluster_id=cluster_id, am_silence_id="sil-a", team_id=team_id)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(f"/api/v1/silences?cluster_id={cluster_id}")
    assert response.status_code == 200
    assert {s["id"] for s in response.json()["silences"]} == {"sil-a", "sil-b"}


async def test_list_team_id_filter_forbidden_for_non_member(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await login_as(client, username="carol")

    response = await client.get(f"/api/v1/silences?cluster_id={cluster_id}&team_id={team_id}")
    assert response.status_code == 403


@respx.mock
async def test_list_alertmanager_down_returns_503(client: AsyncClient) -> None:
    respx.get(SILENCES_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    cluster_id = await _default_cluster_id()
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(f"/api/v1/silences?cluster_id={cluster_id}")
    assert response.status_code == 503


# -- POST / create -------------------------------------------------------


@respx.mock
async def test_create_silence_sends_expected_am_body_and_records_audit(client: AsyncClient) -> None:
    route = respx.post(SILENCES_URL).mock(
        return_value=httpx.Response(200, json={"silenceID": "new-sil-1"})
    )
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    response = await client.post(
        "/api/v1/silences",
        json={
            "cluster_id": cluster_id,
            "team_id": team_id,
            "matchers": [{"name": "alertname", "value": "KamAlwaysFiring", "is_regex": False}],
            "duration_minutes": 60,
            "comment": "testing",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "new-sil-1"
    assert body["team"] == {"id": team_id, "slug": "platform"}
    assert body["status"] == "active"

    sent = json.loads(route.calls[0].request.content)
    assert sent["matchers"] == [
        {"name": "alertname", "value": "KamAlwaysFiring", "isRegex": False, "isEqual": True}
    ]
    assert sent["createdBy"] == "alice"
    assert sent["comment"] == "testing"
    assert "startsAt" in sent and "endsAt" in sent

    async with db_module.async_session_factory() as session:
        result = await session.execute(
            select(SilenceAudit).where(SilenceAudit.am_silence_id == "new-sil-1")
        )
        row = result.scalar_one()
        assert row.team_id == team_id
        assert row.cluster_id == cluster_id
        assert row.comment == "testing"


async def test_create_silence_forbidden_for_non_member(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await login_as(client, username="carol")

    response = await client.post(
        "/api/v1/silences",
        json={
            "cluster_id": cluster_id,
            "team_id": team_id,
            "matchers": [{"name": "alertname", "value": "X", "is_regex": False}],
            "duration_minutes": 60,
            "comment": "testing",
        },
    )
    assert response.status_code == 403


@respx.mock
async def test_create_silence_am_400_maps_to_422_with_am_message_only(client: AsyncClient) -> None:
    respx.post(SILENCES_URL).mock(
        return_value=httpx.Response(400, json={"message": "invalid matcher"})
    )
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    response = await client.post(
        "/api/v1/silences",
        json={
            "cluster_id": cluster_id,
            "team_id": team_id,
            "matchers": [{"name": "alertname", "value": "X", "is_regex": False}],
            "duration_minutes": 60,
            "comment": "testing",
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid matcher"


@respx.mock
async def test_create_silence_am_down_returns_503(client: AsyncClient) -> None:
    respx.post(SILENCES_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    response = await client.post(
        "/api/v1/silences",
        json={
            "cluster_id": cluster_id,
            "team_id": team_id,
            "matchers": [{"name": "alertname", "value": "X", "is_regex": False}],
            "duration_minutes": 60,
            "comment": "testing",
        },
    )
    assert response.status_code == 503


@respx.mock
async def test_create_silence_db_failure_triggers_compensating_expire(client: AsyncClient) -> None:
    respx.post(SILENCES_URL).mock(
        return_value=httpx.Response(200, json={"silenceID": "orphan-sil"})
    )
    expire_route = respx.delete(f"{AM_BASE}/silence/orphan-sil").mock(
        return_value=httpx.Response(200, json={})
    )
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    with patch("app.api.silences.audit.log", side_effect=RuntimeError("db exploded")):
        response = await client.post(
            "/api/v1/silences",
            json={
                "cluster_id": cluster_id,
                "team_id": team_id,
                "matchers": [{"name": "alertname", "value": "X", "is_regex": False}],
                "duration_minutes": 60,
                "comment": "testing",
            },
        )

    assert response.status_code == 500
    assert expire_route.called

    async with db_module.async_session_factory() as session:
        result = await session.execute(
            select(SilenceAudit).where(SilenceAudit.am_silence_id == "orphan-sil")
        )
        assert result.scalar_one_or_none() is None


# -- DELETE / expire -------------------------------------------------------


@respx.mock
async def test_expire_team_member_can_expire_own_teams_silence(client: AsyncClient) -> None:
    delete_route = respx.delete(f"{AM_BASE}/silence/sil-1").mock(return_value=httpx.Response(200, json={}))
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await _insert_audit(cluster_id=cluster_id, am_silence_id="sil-1", team_id=team_id)
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    response = await client.delete(f"/api/v1/silences/sil-1?cluster_id={cluster_id}")
    assert response.status_code == 204
    assert delete_route.called


@respx.mock
async def test_expire_other_teams_member_is_forbidden(client: AsyncClient) -> None:
    respx.delete(f"{AM_BASE}/silence/sil-1").mock(return_value=httpx.Response(200, json={}))
    cluster_id = await _default_cluster_id()
    owning_team_id = await _create_team("platform")
    other_team_id = await _create_team("payments")
    await _insert_audit(cluster_id=cluster_id, am_silence_id="sil-1", team_id=owning_team_id)
    await login_as(client, username="carol")
    await _add_membership(client, other_team_id)

    response = await client.delete(f"/api/v1/silences/sil-1?cluster_id={cluster_id}")
    assert response.status_code == 403


@respx.mock
async def test_expire_external_silence_forbidden_for_non_admin(client: AsyncClient) -> None:
    respx.delete(f"{AM_BASE}/silence/sil-ext").mock(return_value=httpx.Response(200, json={}))
    cluster_id = await _default_cluster_id()
    await login_as(client, username="bob")

    response = await client.delete(f"/api/v1/silences/sil-ext?cluster_id={cluster_id}")
    assert response.status_code == 403


@respx.mock
async def test_expire_external_silence_allowed_for_admin(client: AsyncClient) -> None:
    delete_route = respx.delete(f"{AM_BASE}/silence/sil-ext").mock(return_value=httpx.Response(200, json={}))
    cluster_id = await _default_cluster_id()
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/silences/sil-ext?cluster_id={cluster_id}")
    assert response.status_code == 204
    assert delete_route.called


@respx.mock
async def test_expire_admin_can_expire_any_teams_silence(client: AsyncClient) -> None:
    delete_route = respx.delete(f"{AM_BASE}/silence/sil-1").mock(return_value=httpx.Response(200, json={}))
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await _insert_audit(cluster_id=cluster_id, am_silence_id="sil-1", team_id=team_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/silences/sil-1?cluster_id={cluster_id}")
    assert response.status_code == 204
    assert delete_route.called


@respx.mock
async def test_expire_tolerates_am_404(client: AsyncClient) -> None:
    respx.delete(f"{AM_BASE}/silence/gone-already").mock(return_value=httpx.Response(404))
    cluster_id = await _default_cluster_id()
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/silences/gone-already?cluster_id={cluster_id}")
    assert response.status_code == 204


@respx.mock
async def test_expire_am_down_returns_503(client: AsyncClient) -> None:
    respx.delete(f"{AM_BASE}/silence/sil-1").mock(side_effect=httpx.ConnectError("connection refused"))
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    await _insert_audit(cluster_id=cluster_id, am_silence_id="sil-1", team_id=team_id)
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    response = await client.delete(f"/api/v1/silences/sil-1?cluster_id={cluster_id}")
    assert response.status_code == 503


# -- GET / list: multi-cluster fan-out --------------------------------------


async def _create_cluster(name: str) -> int:
    async with db_module.async_session_factory() as session:
        cluster = Cluster(
            name=name,
            display_name=name,
            prometheus_url="http://prom-2",
            alertmanager_url="http://am-2",
            webhook_token_hash=f"hash-{name}",
        )
        session.add(cluster)
        await session.commit()
        await session.refresh(cluster)
        return cluster.id


async def test_list_unknown_cluster_id_is_404_single_value_backcompat(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/silences?cluster_id=999999")
    assert response.status_code == 404


@respx.mock
async def test_list_fans_out_across_multiple_cluster_ids_and_tags_cluster(
    client: AsyncClient,
) -> None:
    respx.get(SILENCES_URL).mock(return_value=httpx.Response(200, json=[_raw_silence("sil-a")]))
    other_id = await _create_cluster("other-sil")
    respx.get("http://am-2/api/v2/silences").mock(
        return_value=httpx.Response(200, json=[_raw_silence("sil-b")])
    )
    default_id = await _default_cluster_id()
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(f"/api/v1/silences?cluster_id={default_id}&cluster_id={other_id}")
    assert response.status_code == 200
    body = response.json()
    by_id = {s["id"]: s for s in body["silences"]}
    assert set(by_id) == {"sil-a", "sil-b"}
    assert by_id["sil-a"]["cluster"] == {"id": default_id, "name": get_settings().default_cluster_name}
    assert by_id["sil-b"]["cluster"] == {"id": other_id, "name": "other-sil"}


@respx.mock
async def test_list_defaults_to_all_enabled_clusters_when_cluster_id_omitted(
    client: AsyncClient,
) -> None:
    respx.get(SILENCES_URL).mock(return_value=httpx.Response(200, json=[_raw_silence("sil-a")]))
    await _create_cluster("other-sil-2")
    respx.get("http://am-2/api/v2/silences").mock(
        return_value=httpx.Response(200, json=[_raw_silence("sil-c")])
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/silences")
    assert response.status_code == 200
    assert {s["id"] for s in response.json()["silences"]} == {"sil-a", "sil-c"}


@respx.mock
async def test_list_explicit_disabled_cluster_id_is_silently_excluded_not_404(
    client: AsyncClient,
) -> None:
    """An explicitly-requested cluster_id is still intersected with
    `enabled` -- disabled means silently excluded (matching /alerts/live's
    fan-out default), not a 404. Otherwise the same header ClusterFilter
    selection would scope Silences differently from the live-alerts view.
    """
    respx.get(SILENCES_URL).mock(return_value=httpx.Response(200, json=[_raw_silence("sil-a")]))
    disabled_id = await _create_cluster("disabled-sil")
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, disabled_id)
        cluster.enabled = False
        await session.commit()

    default_id = await _default_cluster_id()
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(
        f"/api/v1/silences?cluster_id={default_id}&cluster_id={disabled_id}"
    )
    assert response.status_code == 200
    assert {s["id"] for s in response.json()["silences"]} == {"sil-a"}
