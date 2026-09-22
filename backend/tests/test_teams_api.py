from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.team import Team, TeamLdapMapping, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _fresh_client(app) -> AsyncClient:
    """A second AsyncClient on the same app/db with its own cookie jar."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _create_team(name: str = "platform") -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=name, name=name.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def test_admin_can_create_team_and_dup_slug_409(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    created = await client.post(
        "/api/v1/teams", json={"slug": "platform", "name": "Platform"}
    )
    assert created.status_code == 201
    assert created.json()["slug"] == "platform"

    dup = await client.post(
        "/api/v1/teams", json={"slug": "platform", "name": "Platform Again"}
    )
    assert dup.status_code == 409


async def test_non_admin_cannot_create_team(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    response = await client.post(
        "/api/v1/teams", json={"slug": "platform", "name": "Platform"}
    )
    assert response.status_code == 403


async def test_slug_is_immutable_on_patch(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await client.post(
        "/api/v1/teams", json={"slug": "platform", "name": "Platform"}
    )
    team_id = created.json()["id"]

    patched = await client.patch(
        f"/api/v1/teams/{team_id}", json={"name": "New Name"}
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "New Name"
    assert patched.json()["slug"] == "platform"

    # PATCH schema doesn't accept slug at all -- extra fields are ignored by
    # default pydantic behavior, so the slug stays untouched even if sent.
    patched_again = await client.patch(
        f"/api/v1/teams/{team_id}", json={"slug": "changed", "name": "Still New"}
    )
    assert patched_again.status_code == 200
    assert patched_again.json()["slug"] == "platform"


async def test_member_can_get_non_member_403(app) -> None:
    team_id = await _create_team("platform")

    async with await _fresh_client(app) as bob_client:
        await login_as(bob_client, username="bob")
        bob_id = (await bob_client.get("/api/v1/auth/me")).json()["id"]

        async with db_module.async_session_factory() as session:
            session.add(
                TeamMembership(
                    team_id=team_id, user_id=bob_id, role="member", origin="manual"
                )
            )
            await session.commit()

        member_get = await bob_client.get(f"/api/v1/teams/{team_id}")
        assert member_get.status_code == 200

    async with await _fresh_client(app) as carol_client:
        await login_as(carol_client, username="carol")
        non_member_get = await carol_client.get(f"/api/v1/teams/{team_id}")
        assert non_member_get.status_code == 403


async def test_owner_can_manage_members_and_mappings(app, client: AsyncClient) -> None:
    team_id = await _create_team("payments")

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    alice_id = (await client.get("/api/v1/auth/me")).json()["id"]

    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(
                team_id=team_id, user_id=alice_id, role="owner", origin="manual"
            )
        )
        await session.commit()

    async with await _fresh_client(app) as carol_client:
        await login_as(carol_client, username="carol")
        carol_id = (await carol_client.get("/api/v1/auth/me")).json()["id"]

    # alice (owner) adds carol as a member.
    add_resp = await client.post(
        f"/api/v1/teams/{team_id}/members", json={"user_id": carol_id, "role": "member"}
    )
    assert add_resp.status_code == 201
    membership_id = add_resp.json()["membership_id"]

    dup_resp = await client.post(
        f"/api/v1/teams/{team_id}/members", json={"user_id": carol_id, "role": "member"}
    )
    assert dup_resp.status_code == 409

    members = await client.get(f"/api/v1/teams/{team_id}/members")
    assert members.status_code == 200
    usernames = {m["username"] for m in members.json()}
    assert {"alice", "carol"} <= usernames

    # alice manages ldap mappings.
    mapping_resp = await client.post(
        f"/api/v1/teams/{team_id}/ldap-mappings",
        json={"ldap_group_dn": "cn=team-payments,ou=groups,dc=example,dc=org", "role": "member"},
    )
    assert mapping_resp.status_code == 201
    mapping_id = mapping_resp.json()["id"]

    mappings = await client.get(f"/api/v1/teams/{team_id}/ldap-mappings")
    assert mappings.status_code == 200
    assert len(mappings.json()) == 1

    del_mapping = await client.delete(
        f"/api/v1/teams/{team_id}/ldap-mappings/{mapping_id}"
    )
    assert del_mapping.status_code == 204

    del_member = await client.delete(
        f"/api/v1/teams/{team_id}/members/{membership_id}"
    )
    assert del_member.status_code == 204


async def test_member_cannot_manage_members(app, client: AsyncClient) -> None:
    team_id = await _create_team("payments2")

    await login_as(client, username="bob")
    bob_id = (await client.get("/api/v1/auth/me")).json()["id"]

    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(
                team_id=team_id, user_id=bob_id, role="member", origin="manual"
            )
        )
        await session.commit()

    resp = await client.post(
        f"/api/v1/teams/{team_id}/members", json={"user_id": bob_id, "role": "member"}
    )
    assert resp.status_code == 403


async def test_admin_passes_every_team_rbac_check(client: AsyncClient) -> None:
    team_id = await _create_team("adminland")

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    assert (await client.get(f"/api/v1/teams/{team_id}")).status_code == 200
    assert (await client.get(f"/api/v1/teams/{team_id}/members")).status_code == 200
    assert (
        await client.get(f"/api/v1/teams/{team_id}/ldap-mappings")
    ).status_code == 200


async def test_delete_team_cleans_up_members_and_mappings(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    alice_id = (await client.get("/api/v1/auth/me")).json()["id"]

    created = await client.post(
        "/api/v1/teams", json={"slug": "doomed", "name": "Doomed"}
    )
    team_id = created.json()["id"]

    await client.post(
        f"/api/v1/teams/{team_id}/members", json={"user_id": alice_id, "role": "owner"}
    )
    await client.post(
        f"/api/v1/teams/{team_id}/ldap-mappings",
        json={"ldap_group_dn": "cn=doomed,ou=groups,dc=example,dc=org", "role": "member"},
    )

    delete_resp = await client.delete(f"/api/v1/teams/{team_id}")
    assert delete_resp.status_code == 204

    async with db_module.async_session_factory() as session:
        leftover_members = (
            await session.execute(
                select(TeamMembership).where(TeamMembership.team_id == team_id)
            )
        ).scalars().all()
        leftover_mappings = (
            await session.execute(
                select(TeamLdapMapping).where(TeamLdapMapping.team_id == team_id)
            )
        ).scalars().all()
        assert leftover_members == []
        assert leftover_mappings == []
