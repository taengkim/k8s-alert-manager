"""API-level tests for GET /alerts/history/export: json vs. ndjson, the
per-format row caps (exercised via monkeypatched small cap constants rather
than actually creating tens of thousands of rows), streaming page count, and
scoping/filter parity with GET /alerts/history.
"""

import json
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.alerts as alerts_module
import app.db as db_module
from app.models.alert import AlertEvent
from app.models.audit import AuditLog
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


async def _create_events(
    n: int, *, cluster_id: int, alertname_prefix: str = "Alert", team_id: int | None = None
) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        base = datetime.now(UTC)
        for i in range(n):
            session.add(
                AlertEvent(
                    cluster_id=cluster_id,
                    cluster_name=cluster.name,
                    fingerprint=f"{alertname_prefix}-{i}",
                    status="firing",
                    alertname=f"{alertname_prefix}{i}",
                    severity="critical",
                    namespace="kam-demo",
                    labels={"alertname": f"{alertname_prefix}{i}"},
                    annotations={},
                    team_id=team_id,
                    starts_at=base,
                    first_received_at=base,
                    last_received_at=base,
                )
            )
        await session.commit()


# -- json format --------------------------------------------------------


async def test_json_export_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/alerts/history/export")
    assert response.status_code == 401


async def test_json_export_returns_envelope(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_events(3, cluster_id=cluster_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment" in response.headers["content-disposition"]
    body = response.json()
    assert body["kam_export_version"] == 1
    assert body["kind"] == "alert_history"
    assert len(body["items"]) == 3
    assert "filters" in body


async def test_json_export_reflects_filters(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_events(2, cluster_id=cluster_id, alertname_prefix="Keep")
    await _create_events(2, cluster_id=cluster_id, alertname_prefix="Drop")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history/export", params={"search": "Keep"})
    body = response.json()
    assert len(body["items"]) == 2
    assert all("Keep" in item["alertname"] for item in body["items"])
    assert body["filters"]["search"] == "Keep"


async def test_json_export_scoping_matches_history(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await _create_events(1, cluster_id=cluster_id, alertname_prefix="Plat", team_id=platform.id)
    await _create_events(1, cluster_id=cluster_id, alertname_prefix="Pay", team_id=payments.id)

    await login_as(client, username="alice")
    await _add_membership(client, platform.id)

    response = await client.get(
        "/api/v1/alerts/history/export", params={"team_id": platform.id}
    )
    body = response.json()
    assert [i["alertname"] for i in body["items"]] == ["Plat0"]


async def test_json_export_non_member_of_requested_team_is_403(client: AsyncClient) -> None:
    team = await _create_team("platform")
    await login_as(client, username="carol")
    response = await client.get(
        "/api/v1/alerts/history/export", params={"team_id": team.id}
    )
    assert response.status_code == 403


async def test_json_export_over_cap_is_400(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(alerts_module, "HISTORY_EXPORT_JSON_CAP", 2)
    cluster_id = await _default_cluster_id()
    await _create_events(3, cluster_id=cluster_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history/export")

    assert response.status_code == 400
    assert "3" in response.json()["detail"]


async def test_json_export_writes_audit_row(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    await _create_events(1, cluster_id=cluster_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    await client.get("/api/v1/alerts/history/export")

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "history.export"))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].detail["format"] == "json"


# -- ndjson format --------------------------------------------------------


async def test_ndjson_export_streams_valid_lines(client: AsyncClient, monkeypatch) -> None:
    # Small page size so a modest row count still exercises multiple pages,
    # without actually creating tens of thousands of rows in the test db.
    monkeypatch.setattr(alerts_module, "HISTORY_EXPORT_PAGE_SIZE", 10)
    cluster_id = await _default_cluster_id()
    await _create_events(25, cluster_id=cluster_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history/export", params={"format": "ndjson"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    lines = [line for line in response.text.split("\n") if line]
    assert len(lines) == 25
    for line in lines:
        row = json.loads(line)
        assert "id" in row and "alertname" in row


async def test_ndjson_export_pages_via_cursor_not_one_giant_query(
    client: AsyncClient, monkeypatch
) -> None:
    """2,500 rows at the real 1,000-row page size take exactly 3 page
    queries (1000, 1000, 500 -- the partial last page is what signals the
    end) plus the one pre-flight COUNT(*) -- never one query per row, and
    never the whole result set fetched at once."""
    cluster_id = await _default_cluster_id()
    await _create_events(2500, cluster_id=cluster_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    original_execute = AsyncSession.execute
    calls = 0

    async def counting_execute(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return await original_execute(self, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", counting_execute)

    response = await client.get("/api/v1/alerts/history/export", params={"format": "ndjson"})

    assert response.status_code == 200
    lines = [line for line in response.text.split("\n") if line]
    assert len(lines) == 2500
    # 1 (get_current_user's own User lookup) + 1 (COUNT(*) pre-flight) + 3
    # paged SELECTs (1000 + 1000 + 500 rows) = 5. The key assertion is the
    # "3", not the fixed overhead around it -- confirmed by re-deriving it:
    # ceil(2500 / HISTORY_EXPORT_PAGE_SIZE) + 1 (the trailing empty-page
    # check) would be 4 if the last page weren't already partial; here the
    # partial 500-row last page itself signals "done", so it's exactly 3.
    page_queries = calls - 2
    assert page_queries == 3


async def test_ndjson_export_over_cap_is_400(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(alerts_module, "HISTORY_EXPORT_NDJSON_CAP", 2)
    cluster_id = await _default_cluster_id()
    await _create_events(3, cluster_id=cluster_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/alerts/history/export", params={"format": "ndjson"})
    assert response.status_code == 400
