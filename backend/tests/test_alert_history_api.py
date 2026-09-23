"""API-level tests for GET /alerts/history and /alerts/history/{id}: team
scoping (mirrors /live), filters, pagination, and detail 404/403.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.cluster import Cluster
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _create_team(slug: str) -> Team:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team


async def _add_membership(client: AsyncClient, team_id: int, role: str = "member") -> None:
    me = (await client.get("/api/v1/auth/me")).json()
    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(team_id=team_id, user_id=me["id"], role=role, origin="manual")
        )
        await session.commit()


async def _default_cluster_id() -> int:
    async with db_module.async_session_factory() as session:
        result = await session.execute(select(Cluster))
        return result.scalars().first().id


async def _create_event(
    *,
    cluster_id: int,
    fingerprint: str,
    alertname: str,
    team_id: int | None,
    status: str = "firing",
    severity: str | None = "critical",
    namespace: str | None = "kam-demo",
    starts_at: datetime | None = None,
    last_received_at: datetime | None = None,
) -> int:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        event = AlertEvent(
            cluster_id=cluster_id,
            cluster_name=cluster.name,
            fingerprint=fingerprint,
            status=status,
            alertname=alertname,
            severity=severity,
            namespace=namespace,
            labels={"alertname": alertname},
            annotations={"summary": "test"},
            team_id=team_id,
            starts_at=starts_at or datetime.now(UTC),
            ends_at=None,
            generator_url="http://prom/graph",
            first_received_at=datetime.now(UTC),
            last_received_at=last_received_at or datetime.now(UTC),
            receive_count=1,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event.id


# -- scoping matrix -----------------------------------------------------


async def test_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/alerts/history")
    assert response.status_code == 401


async def test_non_admin_without_team_id_is_422(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    response = await client.get("/api/v1/alerts/history")
    assert response.status_code == 422


async def test_non_member_of_requested_team_is_403(client: AsyncClient) -> None:
    team = await _create_team("platform")
    await login_as(client, username="carol")
    response = await client.get(f"/api/v1/alerts/history?team_id={team.id}")
    assert response.status_code == 403


async def test_member_sees_only_their_team_events(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await _create_event(
        cluster_id=cluster_id, fingerprint="f1", alertname="PlatformOne", team_id=platform.id
    )
    await _create_event(
        cluster_id=cluster_id, fingerprint="f2", alertname="PaymentsOne", team_id=payments.id
    )

    await login_as(client, username="alice")
    await _add_membership(client, platform.id)

    response = await client.get(f"/api/v1/alerts/history?team_id={platform.id}")
    assert response.status_code == 200
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["PlatformOne"]
    assert body["total"] == 1


async def test_admin_without_team_id_sees_all_including_unassigned(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    platform = await _create_team("platform")
    await _create_event(
        cluster_id=cluster_id, fingerprint="f1", alertname="PlatformOne", team_id=platform.id
    )
    await _create_event(
        cluster_id=cluster_id, fingerprint="f2", alertname="Unassigned", team_id=None
    )

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/alerts/history")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {i["alertname"] for i in body["items"]} == {"PlatformOne", "Unassigned"}


# -- filters + pagination -------------------------------------------------


async def test_status_filter(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_event(
        cluster_id=cluster_id,
        fingerprint="f1",
        alertname="Firing",
        team_id=None,
        status="firing",
    )
    await _create_event(
        cluster_id=cluster_id,
        fingerprint="f2",
        alertname="Resolved",
        team_id=None,
        status="resolved",
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history?status=resolved")
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["Resolved"]


async def test_severity_filter_accepts_none_synthetic_value(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_event(
        cluster_id=cluster_id,
        fingerprint="f1",
        alertname="Critical",
        team_id=None,
        severity="critical",
    )
    await _create_event(
        cluster_id=cluster_id,
        fingerprint="f2",
        alertname="NoSeverity",
        team_id=None,
        severity=None,
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history?severity=none")
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["NoSeverity"]


async def test_namespace_filter(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_event(
        cluster_id=cluster_id,
        fingerprint="f1",
        alertname="A",
        team_id=None,
        namespace="ns-a",
    )
    await _create_event(
        cluster_id=cluster_id,
        fingerprint="f2",
        alertname="B",
        team_id=None,
        namespace="ns-b",
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history?namespace=ns-a")
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["A"]


async def test_search_filter_is_case_insensitive_substring(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_event(cluster_id=cluster_id, fingerprint="f1", alertname="PlatformCritical", team_id=None)
    await _create_event(cluster_id=cluster_id, fingerprint="f2", alertname="PaymentsWarn", team_id=None)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history?search=platform")
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["PlatformCritical"]


async def test_cluster_id_filter(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    async with db_module.async_session_factory() as session:
        other = Cluster(
            name="other",
            display_name="other",
            prometheus_url="http://prom",
            alertmanager_url="http://am",
            webhook_token_hash="other-hash",
        )
        session.add(other)
        await session.commit()
        await session.refresh(other)
        other_id = other.id

    await _create_event(cluster_id=cluster_id, fingerprint="f1", alertname="Local", team_id=None)
    await _create_event(cluster_id=other_id, fingerprint="f2", alertname="Other", team_id=None)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(f"/api/v1/alerts/history?cluster_id={other_id}")
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["Other"]


async def test_pagination(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    base = datetime.now(UTC)
    for i in range(5):
        await _create_event(
            cluster_id=cluster_id,
            fingerprint=f"f{i}",
            alertname=f"Alert{i}",
            team_id=None,
            last_received_at=base + timedelta(seconds=i),
        )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history?page=1&page_size=2")
    body = response.json()
    assert body["total"] == 5
    assert body["page"] == 1
    assert body["page_size"] == 2
    # last_received_at desc -> Alert4 first
    assert [i["alertname"] for i in body["items"]] == ["Alert4", "Alert3"]

    response2 = await client.get("/api/v1/alerts/history?page=2&page_size=2")
    body2 = response2.json()
    assert [i["alertname"] for i in body2["items"]] == ["Alert2", "Alert1"]


async def test_page_size_over_max_is_rejected(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/alerts/history?page_size=500")
    assert response.status_code == 422


async def test_history_item_timestamps_are_offset_aware(client: AsyncClient) -> None:
    """C1 regression: on SQLite, a plain DateTime(timezone=True) drops the
    UTC offset on read, so the JSON response would carry an offset-less
    string that the frontend's dayjs(...) parses as *local* time. Every
    timestamp field must serialize with an explicit UTC offset.
    """
    cluster_id = await _default_cluster_id()
    await _create_event(cluster_id=cluster_id, fingerprint="f1", alertname="A", team_id=None)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history")
    item = response.json()["items"][0]
    for field in ("starts_at", "first_received_at", "last_received_at"):
        value = item[field]
        assert value.endswith(("+00:00", "Z")), f"{field}={value!r} has no UTC offset"


async def test_pagination_tiebreaks_on_id_when_last_received_at_ties(
    client: AsyncClient,
) -> None:
    cluster_id = await _default_cluster_id()
    same_ts = datetime.now(UTC)
    id1 = await _create_event(
        cluster_id=cluster_id,
        fingerprint="f1",
        alertname="First",
        team_id=None,
        last_received_at=same_ts,
    )
    id2 = await _create_event(
        cluster_id=cluster_id,
        fingerprint="f2",
        alertname="Second",
        team_id=None,
        last_received_at=same_ts,
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history")
    body = response.json()
    assert [i["id"] for i in body["items"]] == sorted([id1, id2], reverse=True)


async def test_search_filter_escapes_percent_as_literal(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_event(cluster_id=cluster_id, fingerprint="f1", alertname="cpu%usage", team_id=None)
    await _create_event(
        cluster_id=cluster_id, fingerprint="f2", alertname="cpu_usage_alt", team_id=None
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history", params={"search": "cpu%usage"})
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["cpu%usage"]


async def test_range_filter_normalizes_non_utc_offsets(client: AsyncClient) -> None:
    # A +09:00-spelled from_ts must compare as the same instant as its UTC
    # form: UTCDateTime.process_bind_param normalizes aware params to UTC
    # before SQLite drops the offset.
    cluster_id = await _default_cluster_id()
    instant = datetime(2026, 9, 23, 9, 0, 0, tzinfo=UTC)
    await _create_event(
        cluster_id=cluster_id,
        fingerprint="f-range",
        alertname="RangeAlert",
        team_id=None,
        last_received_at=instant,
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    # 18:00+09:00 == 09:00Z — the event at exactly 09:00Z must be included.
    response = await client.get(
        "/api/v1/alerts/history", params={"from_ts": "2026-09-23T18:00:00+09:00"}
    )
    assert response.status_code == 200
    assert [i["alertname"] for i in response.json()["items"]] == ["RangeAlert"]

    # One second later in +09:00 excludes it.
    response = await client.get(
        "/api/v1/alerts/history", params={"from_ts": "2026-09-23T18:00:01+09:00"}
    )
    assert response.json()["items"] == []


# -- detail ---------------------------------------------------------------


async def test_detail_not_found_is_404(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/alerts/history/999999")
    assert response.status_code == 404


async def test_detail_forbidden_for_non_member(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    platform = await _create_team("platform")
    event_id = await _create_event(
        cluster_id=cluster_id, fingerprint="f1", alertname="PlatformOne", team_id=platform.id
    )
    await login_as(client, username="carol")

    response = await client.get(f"/api/v1/alerts/history/{event_id}")
    assert response.status_code == 403


async def test_detail_unassigned_forbidden_for_non_admin(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    event_id = await _create_event(
        cluster_id=cluster_id, fingerprint="f1", alertname="Unassigned", team_id=None
    )
    await login_as(client, username="bob")

    response = await client.get(f"/api/v1/alerts/history/{event_id}")
    assert response.status_code == 403


async def test_detail_allowed_for_team_member(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    platform = await _create_team("platform")
    event_id = await _create_event(
        cluster_id=cluster_id, fingerprint="f1", alertname="PlatformOne", team_id=platform.id
    )
    await login_as(client, username="alice")
    await _add_membership(client, platform.id)

    response = await client.get(f"/api/v1/alerts/history/{event_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["alertname"] == "PlatformOne"
    assert "labels" in body
    assert "annotations" in body


async def test_detail_allowed_for_admin_on_unassigned(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    event_id = await _create_event(
        cluster_id=cluster_id, fingerprint="f1", alertname="Unassigned", team_id=None
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get(f"/api/v1/alerts/history/{event_id}")
    assert response.status_code == 200
