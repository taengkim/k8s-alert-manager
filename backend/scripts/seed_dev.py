"""Idempotent local-dev seed: migrates to head, then upserts an admin user
(alice), the `platform` team, alice's owner membership, and its LDAP group
mapping.

Run via `make seed-dev` (or `cd backend && uv run python -m scripts.seed_dev`).

Safe to re-run any time. Manual memberships are never touched by the LDAP
group-sync path (see app/services/auth_sync.py), so alice's `origin="manual"`
membership seeded here coexists fine with logging in via LDAP afterwards --
sync only adds/removes `origin="ldap"` memberships.
"""

import asyncio
from pathlib import Path

from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.db import async_session_factory
from app.models.team import Team, TeamLdapMapping, TeamMembership
from app.models.user import User

BACKEND_DIR = Path(__file__).resolve().parent.parent
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"

ADMIN_USERNAME = "alice"
ADMIN_DISPLAY_NAME = "Alice Kim"
PLATFORM_SLUG = "platform"
PLATFORM_LDAP_GROUP_DN = "cn=team-platform,ou=groups,dc=example,dc=org"


def _run_migrations() -> None:
    cfg = Config(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")


async def _upsert_admin_user(session: AsyncSession) -> User:
    result = await session.execute(select(User).where(User.username == ADMIN_USERNAME))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(username=ADMIN_USERNAME, display_name=ADMIN_DISPLAY_NAME, is_admin=True)
        session.add(user)
        await session.flush()
    else:
        user.display_name = ADMIN_DISPLAY_NAME
        user.is_admin = True
    return user


async def _upsert_platform_team(session: AsyncSession) -> Team:
    result = await session.execute(select(Team).where(Team.slug == PLATFORM_SLUG))
    team = result.scalar_one_or_none()
    if team is None:
        team = Team(slug=PLATFORM_SLUG, name=PLATFORM_SLUG.title())
        session.add(team)
        await session.flush()
    return team


async def _upsert_owner_membership(session: AsyncSession, team: Team, user: User) -> None:
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team.id, TeamMembership.user_id == user.id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        session.add(
            TeamMembership(team_id=team.id, user_id=user.id, role="owner", origin="manual")
        )
    else:
        membership.role = "owner"
        membership.origin = "manual"


async def _upsert_ldap_mapping(session: AsyncSession, team: Team) -> None:
    result = await session.execute(
        select(TeamLdapMapping).where(
            TeamLdapMapping.team_id == team.id,
            TeamLdapMapping.ldap_group_dn == PLATFORM_LDAP_GROUP_DN,
        )
    )
    mapping = result.scalar_one_or_none()
    if mapping is None:
        session.add(
            TeamLdapMapping(
                team_id=team.id, ldap_group_dn=PLATFORM_LDAP_GROUP_DN, role="member"
            )
        )
    else:
        mapping.role = "member"


async def _seed() -> None:
    async with async_session_factory() as session:
        user = await _upsert_admin_user(session)
        team = await _upsert_platform_team(session)
        await _upsert_owner_membership(session, team, user)
        await _upsert_ldap_mapping(session, team)
        await session.commit()


def main() -> None:
    print("==> running migrations to head")
    _run_migrations()

    print("==> seeding admin user + platform team")
    asyncio.run(_seed())

    print(f"==> seed complete: user={ADMIN_USERNAME} (admin) team={PLATFORM_SLUG} (alice=owner)")


if __name__ == "__main__":
    main()
