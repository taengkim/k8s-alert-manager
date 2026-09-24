"""API-level tests for /api/v1/stats/*: the same team-scoping matrix as
/alerts (non-admin without team_id -> 422, non-member -> 403, admin
unscoped -> everything), plus the 90-day range cap and a couple of
correctness spot-checks confirming the API wires the service correctly.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.cluster import Cluster
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"
NOW = datetime.now(UTC)


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


async def _create_cluster(name: str = "stats-api-cluster") -> int:
    async with db_module.async_session_factory() as session:
        cluster = Cluster(
            name=name,
            display_name=name,
            prometheus_url="http://prom",
            alertmanager_url="http://am",
            webhook_token_hash=f"hash-{name}",
        )
        session.add(cluster)
        await session.commit()
        await session.refresh(cluster)
        return cluster.id


async def _seed_event(cluster_id: int, team_id: int | None, *, alertname: str = "HighCpu") -> None:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        session.add(
            AlertEvent(
                cluster_id=cluster_id,
                cluster_name=cluster.name,
                fingerprint=f"fp-{alertname}-{team_id}",
                status="firing",
                alertname=alertname,
                severity="critical",
                namespace="kam-demo",
                labels={"alertname": alertname},
                annotations={},
                team_id=team_id,
                starts_at=NOW,
                first_received_at=NOW,
                last_received_at=NOW,
            )
        )
        await session.commit()


# -- scoping matrix (mirrors /alerts/live's) -------------------------------


async def test_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/stats/summary")
    assert response.status_code == 401


async def test_non_admin_without_team_id_is_422(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    response = await client.get("/api/v1/stats/top-alerts")
    assert response.status_code == 422


async def test_non_member_of_requested_team_is_403(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    await login_as(client, username="carol")
    response = await client.get(f"/api/v1/stats/summary?team_id={team_id}")
    assert response.status_code == 403


async def test_member_sees_only_their_team_data(client: AsyncClient) -> None:
    team_a = await _create_team("platform")
    team_b = await _create_team("payments")
    cluster_id = await _create_cluster()
    await _seed_event(cluster_id, team_a, alertname="PlatformAlert")
    await _seed_event(cluster_id, team_b, alertname="PaymentsAlert")

    await login_as(client, username="alice")
    await _add_membership(client, team_a)

    response = await client.get(f"/api/v1/stats/top-alerts?team_id={team_a}")
    assert response.status_code == 200
    body = response.json()
    assert [row["alertname"] for row in body] == ["PlatformAlert"]


async def test_admin_without_team_id_sees_all(client: AsyncClient) -> None:
    team_a = await _create_team("platform")
    team_b = await _create_team("payments")
    cluster_id = await _create_cluster()
    await _seed_event(cluster_id, team_a, alertname="PlatformAlert")
    await _seed_event(cluster_id, team_b, alertname="PaymentsAlert")

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/stats/top-alerts")
    assert response.status_code == 200
    body = response.json()
    assert {row["alertname"] for row in body} == {"PlatformAlert", "PaymentsAlert"}


async def test_admin_sees_unassigned_events_too(client: AsyncClient) -> None:
    cluster_id = await _create_cluster()
    await _seed_event(cluster_id, None, alertname="Unassigned")

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/stats/summary")
    assert response.status_code == 200
    assert response.json()["events_in_range"] == 1


# -- range validation -------------------------------------------------------


async def test_range_exceeding_90_days_is_422(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    from_ts = (NOW - timedelta(days=91)).isoformat()
    to_ts = NOW.isoformat()
    # Via `params=`, not an f-string URL: isoformat()'s "+00:00" offset must
    # be percent-encoded ("+" otherwise decodes as a literal space and the
    # datetime fails to parse at all -- a false-positive 422 for the wrong
    # reason).
    response = await client.get(
        "/api/v1/stats/summary", params={"from_ts": from_ts, "to_ts": to_ts}
    )
    assert response.status_code == 422


async def test_default_range_requires_no_params(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/stats/summary")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "firing_now",
        "events_in_range",
        "delivered_in_range",
        "failed_or_dead_in_range",
    }


async def test_zero_width_range_is_not_rejected(client: AsyncClient) -> None:
    """An empty period (from_ts == to_ts) is a valid query, not a 422 --
    see app.api.stats._resolve_range's docstring.
    """
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    ts = NOW.isoformat()
    response = await client.get("/api/v1/stats/summary", params={"from_ts": ts, "to_ts": ts})
    assert response.status_code == 200
    assert response.json()["events_in_range"] == 0


# -- breakdown-specific ------------------------------------------------------


async def test_breakdown_requires_by_param(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/stats/breakdown")
    assert response.status_code == 422


async def test_breakdown_by_severity(client: AsyncClient) -> None:
    cluster_id = await _create_cluster()
    await _seed_event(cluster_id, None)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/stats/breakdown?by=severity")
    assert response.status_code == 200
    assert response.json() == [{"key": "critical", "count": 1}]


async def test_volume_bucket_query_param(client: AsyncClient) -> None:
    cluster_id = await _create_cluster()
    await _seed_event(cluster_id, None)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/stats/volume?bucket=hour")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["firing_count"] == 1
