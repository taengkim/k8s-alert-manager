from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.team import Team, TeamLdapMapping, TeamMembership
from app.models.user import User
from tests.conftest import login_as

PLATFORM_DN = "cn=team-platform,ou=groups,dc=example,dc=org"


async def _create_team_and_mapping(role: str = "member") -> tuple[int, int]:
    async with db_module.async_session_factory() as session:
        team = Team(slug="platform", name="Platform")
        session.add(team)
        await session.flush()
        mapping = TeamLdapMapping(
            team_id=team.id, ldap_group_dn=PLATFORM_DN, role=role
        )
        session.add(mapping)
        await session.commit()
        return team.id, mapping.id


async def test_mapping_match_creates_ldap_membership(client: AsyncClient) -> None:
    team_id, _ = await _create_team_and_mapping(role="member")

    response = await login_as(client, username="alice", group_dns=[PLATFORM_DN])
    assert response.status_code == 200

    async with db_module.async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
                )
            )
        ).scalar_one()
        assert membership.origin == "ldap"
        assert membership.role == "member"


async def test_group_removed_on_next_login_removes_ldap_row(client: AsyncClient) -> None:
    team_id, _ = await _create_team_and_mapping(role="member")

    await login_as(client, username="alice", group_dns=[PLATFORM_DN])
    await client.post("/api/v1/auth/logout")

    # Second login: alice no longer belongs to team-platform.
    await login_as(client, username="alice", group_dns=[])

    async with db_module.async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        result = await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
            )
        )
        assert result.scalar_one_or_none() is None


async def test_manual_membership_survives_sync(client: AsyncClient) -> None:
    team_id, _ = await _create_team_and_mapping(role="member")

    # alice logs in without the group -> no ldap membership created.
    await login_as(client, username="alice", group_dns=[])

    async with db_module.async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        session.add(
            TeamMembership(
                team_id=team_id, user_id=user.id, role="owner", origin="manual"
            )
        )
        await session.commit()

    await client.post("/api/v1/auth/logout")
    # Login again, still without the group.
    await login_as(client, username="alice", group_dns=[])

    async with db_module.async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
                )
            )
        ).scalar_one()
        assert membership.origin == "manual"
        assert membership.role == "owner"


async def test_role_from_mapping_is_honored(client: AsyncClient) -> None:
    team_id, _ = await _create_team_and_mapping(role="owner")

    await login_as(client, username="alice", group_dns=[PLATFORM_DN])

    async with db_module.async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        membership = (
            await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
                )
            )
        ).scalar_one()
        assert membership.role == "owner"
        assert membership.origin == "ldap"


async def test_two_mappings_to_same_team_do_not_duplicate_membership(
    client: AsyncClient,
) -> None:
    """Two different LDAP groups can both grant access to one team. Both
    matching on the same login must not attempt a second insert for the same
    (team_id, user_id) -- that would violate the uq_team_user constraint.
    """
    other_dn = "cn=team-platform-secondary,ou=groups,dc=example,dc=org"
    team_id, _ = await _create_team_and_mapping(role="member")

    async with db_module.async_session_factory() as session:
        session.add(
            TeamLdapMapping(team_id=team_id, ldap_group_dn=other_dn, role="owner")
        )
        await session.commit()

    response = await login_as(
        client, username="alice", group_dns=[PLATFORM_DN, other_dn]
    )
    assert response.status_code == 200

    async with db_module.async_session_factory() as session:
        user = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        rows = (
            await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
                )
            )
        ).scalars().all()
        assert len(rows) == 1
