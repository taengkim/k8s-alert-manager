"""API-level tests for GET /api/v1/audit: the admin/owner/member scoping
matrix (stricter than /alerts and /stats -- a plain member is 403'd
outright, not merely narrowed), the action-prefix filter, and the
username join.
"""

from datetime import UTC, datetime

from httpx import AsyncClient

import app.db as db_module
from app.models.audit import AuditLog
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


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


async def _seed_row(
    *, team_id: int | None, user_id: int | None, action: str, object_ref: str = "x"
) -> None:
    async with db_module.async_session_factory() as session:
        session.add(
            AuditLog(
                user_id=user_id,
                team_id=team_id,
                action=action,
                object_type="test",
                object_ref=object_ref,
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()


async def test_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/audit")
    assert response.status_code == 401


async def test_member_is_403(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    await login_as(client, username="carol")
    await _add_membership(client, team_id, role="member")

    response = await client.get(f"/api/v1/audit?team_id={team_id}")
    assert response.status_code == 403


async def test_owner_without_team_id_is_422(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    await login_as(client, username="dave")
    await _add_membership(client, team_id, role="owner")

    response = await client.get("/api/v1/audit")
    assert response.status_code == 422


async def test_owner_of_other_team_is_403(client: AsyncClient) -> None:
    own_team = await _create_team("platform")
    other_team = await _create_team("payments")
    await login_as(client, username="dave")
    await _add_membership(client, own_team, role="owner")

    response = await client.get(f"/api/v1/audit?team_id={other_team}")
    assert response.status_code == 403


async def test_owner_sees_only_own_team_rows(client: AsyncClient) -> None:
    own_team = await _create_team("platform")
    other_team = await _create_team("payments")
    await _seed_row(team_id=own_team, user_id=None, action="rule.create", object_ref="r1")
    await _seed_row(team_id=other_team, user_id=None, action="rule.create", object_ref="r2")

    await login_as(client, username="dave")
    await _add_membership(client, own_team, role="owner")

    response = await client.get(f"/api/v1/audit?team_id={own_team}")
    assert response.status_code == 200
    body = response.json()
    assert [item["object_ref"] for item in body["items"]] == ["r1"]


async def test_admin_sees_all_teams_and_can_filter(client: AsyncClient) -> None:
    team_a = await _create_team("platform")
    team_b = await _create_team("payments")
    await _seed_row(team_id=team_a, user_id=None, action="rule.create", object_ref="a")
    await _seed_row(team_id=team_b, user_id=None, action="rule.create", object_ref="b")

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response_all = await client.get("/api/v1/audit")
    assert response_all.status_code == 200
    assert response_all.json()["total"] >= 2

    response_scoped = await client.get(f"/api/v1/audit?team_id={team_a}")
    assert response_scoped.status_code == 200
    refs = [item["object_ref"] for item in response_scoped.json()["items"]]
    assert refs == ["a"]


async def test_action_prefix_filter(client: AsyncClient) -> None:
    await _seed_row(team_id=None, user_id=None, action="rule.create", object_ref="r1")
    await _seed_row(team_id=None, user_id=None, action="rule.delete", object_ref="r2")
    await _seed_row(team_id=None, user_id=None, action="silence.create", object_ref="s1")

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/audit?action=rule.")
    assert response.status_code == 200
    refs = {item["object_ref"] for item in response.json()["items"]}
    assert refs == {"r1", "r2"}


async def test_username_join(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    alice_id = (await client.get("/api/v1/auth/me")).json()["id"]

    # login_as itself already wrote an auth.login row for alice -- confirm
    # its username is resolved via the join rather than left null.
    response = await client.get(f"/api/v1/audit?user_id={alice_id}&action=auth.login")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["username"] == "alice"


async def test_pagination_defaults_and_max(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.get("/api/v1/audit")
    assert response.status_code == 200
    assert response.json()["page_size"] == 50

    too_big = await client.get("/api/v1/audit?page_size=201")
    assert too_big.status_code == 422

    max_ok = await client.get("/api/v1/audit?page_size=200")
    assert max_ok.status_code == 200
