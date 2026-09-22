import httpx
import respx
from httpx import AsyncClient

import app.db as db_module
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"
AM_URL = "http://localhost:30093/api/v2/alerts"

SAMPLE_ALERTS = [
    {
        "fingerprint": "f1",
        "labels": {
            "alertname": "PlatformCritical",
            "kam_team": "platform",
            "severity": "critical",
            "namespace": "kam-demo",
        },
        "annotations": {"summary": "platform critical"},
        "status": {"state": "active", "silencedBy": []},
        "startsAt": "2026-09-22T00:00:00Z",
        "generatorURL": "http://prom/graph1",
        "receivers": [{"name": "kam-webhook"}],
    },
    {
        "fingerprint": "f2",
        "labels": {
            "alertname": "PlatformInfo",
            "kam_team": "platform",
            "severity": "info",
            "namespace": "kam-other",
        },
        "annotations": {},
        "status": {"state": "suppressed", "silencedBy": ["sil-1"]},
        "startsAt": "2026-09-22T00:01:00Z",
        "generatorURL": "http://prom/graph2",
        "receivers": [],
    },
    {
        "fingerprint": "f3",
        "labels": {
            "alertname": "PaymentsWarn",
            "kam_team": "payments",
            "severity": "warning",
            "namespace": "kam-payments",
        },
        "annotations": {},
        "status": {"state": "active", "silencedBy": []},
        "startsAt": "2026-09-22T00:02:00Z",
        "generatorURL": "http://prom/graph3",
        "receivers": [],
    },
    {
        "fingerprint": "f4",
        "labels": {
            "alertname": "Unassigned",
            "namespace": "default",
        },
        "annotations": {},
        "status": {"state": "active", "silencedBy": []},
        "startsAt": "2026-09-22T00:03:00Z",
        "generatorURL": "http://prom/graph4",
        "receivers": [],
    },
]


async def _create_team(slug: str) -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(
    client: AsyncClient, team_id: int, role: str = "member"
) -> None:
    """Add the currently-logged-in client's user as a member of `team_id`."""
    me = (await client.get("/api/v1/auth/me")).json()
    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(
                team_id=team_id, user_id=me["id"], role=role, origin="manual"
            )
        )
        await session.commit()


async def test_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/alerts/live")
    assert response.status_code == 401


async def test_non_admin_without_team_id_is_422(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    response = await client.get("/api/v1/alerts/live")
    assert response.status_code == 422


async def test_non_member_of_requested_team_is_403(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    await login_as(client, username="carol")
    response = await client.get(f"/api/v1/alerts/live?team_id={team_id}")
    assert response.status_code == 403


@respx.mock
async def test_member_sees_only_their_team_alerts(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    team_id = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    response = await client.get(f"/api/v1/alerts/live?team_id={team_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert {a["alertname"] for a in body["alerts"]} == {"PlatformCritical", "PlatformInfo"}


@respx.mock
async def test_payments_member_sees_only_payments_alerts(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    team_id = await _create_team("payments")
    await login_as(client, username="carol")
    await _add_membership(client, team_id)

    response = await client.get(f"/api/v1/alerts/live?team_id={team_id}")
    assert response.status_code == 200
    body = response.json()
    assert [a["alertname"] for a in body["alerts"]] == ["PaymentsWarn"]


@respx.mock
async def test_admin_without_team_id_sees_all_alerts_including_unlabeled(
    client: AsyncClient,
) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live")
    assert response.status_code == 200
    body = response.json()
    assert len(body["alerts"]) == 4
    assert "Unassigned" in {a["alertname"] for a in body["alerts"]}


@respx.mock
async def test_severity_filter_narrows_results(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live?severity=critical")
    body = response.json()
    assert [a["alertname"] for a in body["alerts"]] == ["PlatformCritical"]


@respx.mock
async def test_severity_filter_accepts_comma_separated_multi(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live?severity=critical,info")
    body = response.json()
    assert {a["alertname"] for a in body["alerts"]} == {"PlatformCritical", "PlatformInfo"}


@respx.mock
async def test_severity_none_filter_matches_unlabeled_alerts(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live?severity=none")
    body = response.json()
    assert [a["alertname"] for a in body["alerts"]] == ["Unassigned"]


@respx.mock
async def test_namespace_filter_narrows_results(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live?namespace=kam-payments")
    body = response.json()
    assert [a["alertname"] for a in body["alerts"]] == ["PaymentsWarn"]


@respx.mock
async def test_state_filter_narrows_results(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live?state=suppressed")
    body = response.json()
    assert [a["alertname"] for a in body["alerts"]] == ["PlatformInfo"]


@respx.mock
async def test_search_filter_is_case_insensitive_substring_on_alertname(
    client: AsyncClient,
) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live?search=platform")
    body = response.json()
    assert {a["alertname"] for a in body["alerts"]} == {"PlatformCritical", "PlatformInfo"}


@respx.mock
async def test_alertmanager_connection_error_returns_partial_failure(
    client: AsyncClient,
) -> None:
    respx.get(AM_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live")
    assert response.status_code == 200
    body = response.json()
    assert body["alerts"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["cluster"] == "local"


@respx.mock
async def test_alertmanager_non_json_body_returns_partial_failure(
    client: AsyncClient,
) -> None:
    """A misbehaving proxy/gateway in front of Alertmanager could return a
    200 with an HTML error page instead of JSON. That must degrade to an
    errors[] entry for this cluster, not 500 the whole endpoint.
    """
    respx.get(AM_URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html>not json</html>"
        )
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live")
    assert response.status_code == 200
    body = response.json()
    assert body["alerts"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["cluster"] == "local"


@respx.mock
async def test_alertmanager_non_list_json_body_returns_partial_failure(
    client: AsyncClient,
) -> None:
    """AM's v2 alerts endpoint should always return a JSON array, but a
    malformed/incompatible response (e.g. a JSON object) must also degrade
    to an errors[] entry rather than raising while flattening.
    """
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json={"error": "boom"}))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live")
    assert response.status_code == 200
    body = response.json()
    assert body["alerts"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["cluster"] == "local"
