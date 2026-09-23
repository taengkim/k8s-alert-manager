import httpx
import respx
from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.config import get_settings
from app.models.cluster import Cluster
from app.models.share import AlertShare
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


# -- cluster_id[] filter + grafana_url ---------------------------------------


async def _default_cluster_id() -> int:
    async with db_module.async_session_factory() as session:
        result = await session.execute(
            select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
        )
        return result.scalar_one().id


async def _create_cluster(name: str, *, grafana_url: str | None = None) -> Cluster:
    async with db_module.async_session_factory() as session:
        cluster = Cluster(
            name=name,
            display_name=name,
            prometheus_url="http://prom-2",
            alertmanager_url="http://am-2",
            grafana_url=grafana_url,
            webhook_token_hash=f"hash-{name}",
        )
        session.add(cluster)
        await session.commit()
        await session.refresh(cluster)
        return cluster


@respx.mock
async def test_cluster_id_filter_narrows_fan_out_to_requested_enabled_clusters(
    client: AsyncClient,
) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    other = await _create_cluster("other-live")
    other_route = respx.get("http://am-2/api/v2/alerts").mock(
        return_value=httpx.Response(200, json=[])
    )
    default_id = await _default_cluster_id()

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get(f"/api/v1/alerts/live?cluster_id={other.id}")
    assert response.status_code == 200
    # Only the requested cluster is queried -- the default cluster's alerts
    # (SAMPLE_ALERTS) must not appear.
    assert response.json()["alerts"] == []
    assert other_route.called

    response_default = await client.get(f"/api/v1/alerts/live?cluster_id={default_id}")
    assert len(response_default.json()["alerts"]) == 4


@respx.mock
async def test_live_alert_grafana_url_falls_back_to_cluster(client: AsyncClient) -> None:
    cluster = await _create_cluster(
        "grafana-live", grafana_url="https://cluster-grafana.example.com"
    )
    respx.get("http://am-2/api/v2/alerts").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "fingerprint": "gf1",
                    "labels": {"alertname": "NoAnnotationAlert"},
                    "annotations": {},
                    "status": {"state": "active", "silencedBy": []},
                    "startsAt": "2026-09-22T00:00:00Z",
                    "generatorURL": "http://prom/graph",
                }
            ],
        )
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(f"/api/v1/alerts/live?cluster_id={cluster.id}")
    assert response.status_code == 200
    alert = response.json()["alerts"][0]
    assert alert["grafana_url"] == (
        "https://cluster-grafana.example.com/alerting/list?queryString=NoAnnotationAlert"
    )


@respx.mock
async def test_live_alert_grafana_url_annotation_wins_over_cluster(client: AsyncClient) -> None:
    cluster = await _create_cluster(
        "grafana-live-2", grafana_url="https://cluster-grafana.example.com"
    )
    respx.get("http://am-2/api/v2/alerts").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "fingerprint": "gf2",
                    "labels": {"alertname": "AnnotatedAlert"},
                    "annotations": {"kam_grafana_url": "https://direct-link.example.com/d/x"},
                    "status": {"state": "active", "silencedBy": []},
                    "startsAt": "2026-09-22T00:00:00Z",
                    "generatorURL": "http://prom/graph",
                }
            ],
        )
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(f"/api/v1/alerts/live?cluster_id={cluster.id}")
    alert = response.json()["alerts"][0]
    assert alert["grafana_url"] == "https://direct-link.example.com/d/x"


# -- Phase 14: shared visibility ---------------------------------------------


@respx.mock
async def test_own_team_alerts_have_shared_from_null(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    team_id = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, team_id)

    response = await client.get(f"/api/v1/alerts/live?team_id={team_id}")
    body = response.json()
    assert {a["shared_from"] for a in body["alerts"]} == {None}


@respx.mock
async def test_view_share_exposes_owners_alerts_with_shared_from(client: AsyncClient) -> None:
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    platform_id = await _create_team("platform")
    payments_id = await _create_team("payments")
    async with db_module.async_session_factory() as session:
        session.add(AlertShare(owner_team_id=platform_id, target_team_id=payments_id, mode="view"))
        await session.commit()

    await login_as(client, username="carol")
    await _add_membership(client, payments_id)

    response = await client.get(f"/api/v1/alerts/live?team_id={payments_id}")
    assert response.status_code == 200
    body = response.json()
    by_name = {a["alertname"]: a for a in body["alerts"]}
    assert set(by_name) == {"PaymentsWarn", "PlatformCritical", "PlatformInfo"}
    assert by_name["PaymentsWarn"]["shared_from"] is None
    assert by_name["PlatformCritical"]["shared_from"] == "platform"
    assert by_name["PlatformInfo"]["shared_from"] == "platform"


@respx.mock
async def test_share_with_no_matching_share_keeps_other_teams_alerts_hidden(
    client: AsyncClient,
) -> None:
    """No AlertShare at all between the two teams -- unchanged pre-Phase-14
    behavior: payments never sees platform's alerts.
    """
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await _create_team("platform")
    payments_id = await _create_team("payments")
    await login_as(client, username="carol")
    await _add_membership(client, payments_id)

    response = await client.get(f"/api/v1/alerts/live?team_id={payments_id}")
    body = response.json()
    assert [a["alertname"] for a in body["alerts"]] == ["PaymentsWarn"]


@respx.mock
async def test_share_matcher_scope_narrows_shared_alerts(client: AsyncClient) -> None:
    """A share with matchers only exposes alerts within that scope --
    PlatformCritical (severity=critical) passes a severity=critical
    include matcher; PlatformInfo (severity=info) doesn't.
    """
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    platform_id = await _create_team("platform")
    payments_id = await _create_team("payments")
    async with db_module.async_session_factory() as session:
        session.add(
            AlertShare(
                owner_team_id=platform_id,
                target_team_id=payments_id,
                mode="view_notify",
                matchers=[
                    {"kind": "include", "target": "label", "key": "severity", "pattern": "^critical$"}
                ],
            )
        )
        await session.commit()

    await login_as(client, username="carol")
    await _add_membership(client, payments_id)

    response = await client.get(f"/api/v1/alerts/live?team_id={payments_id}")
    body = response.json()
    names = {a["alertname"] for a in body["alerts"]}
    assert "PlatformCritical" in names
    assert "PlatformInfo" not in names
    assert "PaymentsWarn" in names


@respx.mock
async def test_admin_unscoped_view_never_sets_shared_from(client: AsyncClient) -> None:
    """The unscoped admin ("all alerts") view isn't "viewing as a team", so
    sharing doesn't apply -- shared_from stays None even for alerts owned
    by a team other than the admin's own.
    """
    respx.get(AM_URL).mock(return_value=httpx.Response(200, json=SAMPLE_ALERTS))
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/live")
    body = response.json()
    assert {a["shared_from"] for a in body["alerts"]} == {None}
