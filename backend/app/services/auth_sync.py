"""Sync a User + team memberships from LDAP directory info on every login."""

import re
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import get_settings
from app.models.team import TeamLdapMapping, TeamMembership
from app.models.user import User
from app.services.ldap_auth import LdapUserInfo

# settings.ldap_admin_groups is a ';'-separated list of DNs, NOT ','-
# separated: a DN is itself comma-separated (e.g.
# "cn=admins,ou=groups,dc=example,dc=org"), so ',' can't double as the list
# delimiter without shredding every DN into its RDN components.
_ADMIN_GROUPS_SPLIT_RE = re.compile(r"\s*;\s*")

# DN comparisons are lowercased and have any whitespace after a comma
# collapsed, so "cn=x, ou=g" and "cn=x,ou=g" are treated as equal.
_COMMA_SPACE_RE = re.compile(r",\s+")


def _normalize_dn(dn: str) -> str:
    return _COMMA_SPACE_RE.sub(",", dn.strip()).lower()


def _is_admin_group(group_dns: list[str]) -> bool:
    settings = get_settings()
    admin_dns = {
        _normalize_dn(dn)
        for dn in _ADMIN_GROUPS_SPLIT_RE.split(settings.ldap_admin_groups)
        if dn.strip()
    }
    return any(_normalize_dn(dn) in admin_dns for dn in group_dns)


async def sync_user(session, info: LdapUserInfo) -> User:
    result = await session.execute(select(User).where(User.username == info.username))
    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            username=info.username,
            display_name=info.display_name,
            email=info.email,
            is_admin=False,
            is_active=True,
        )
        session.add(user)
    else:
        user.display_name = info.display_name
        user.email = info.email

    user.last_login_at = datetime.now(UTC)

    # Never auto-demote: only ever flip False -> True.
    if _is_admin_group(info.group_dns):
        user.is_admin = True

    await session.flush()  # ensure user.id is populated for membership sync

    await _sync_memberships(session, user, info.group_dns)

    return user


async def _sync_memberships(session, user: User, group_dns: list[str]) -> None:
    mapping_result = await session.execute(select(TeamLdapMapping))
    mappings = mapping_result.scalars().all()

    group_dn_set = {_normalize_dn(dn) for dn in group_dns}
    matched_mappings = [m for m in mappings if _normalize_dn(m.ldap_group_dn) in group_dn_set]
    matched_team_ids = {m.team_id for m in matched_mappings}

    # All existing memberships for this user, keyed by team_id, regardless of
    # origin: the unique constraint is (team_id, user_id) so a manual row
    # already occupying a team must block inserting a second ldap-origin row.
    membership_result = await session.execute(
        select(TeamMembership).where(TeamMembership.user_id == user.id)
    )
    existing_by_team = {m.team_id: m for m in membership_result.scalars().all()}

    # Add/update memberships for currently-matched mappings. Two mappings can
    # target the same team_id (different LDAP groups both granting access to
    # one team), so existing_by_team must be updated as we go -- otherwise a
    # second match for a team already inserted this loop would attempt a
    # duplicate (team_id, user_id) insert and violate uq_team_user.
    for mapping in matched_mappings:
        existing = existing_by_team.get(mapping.team_id)
        if existing is None:
            new_membership = TeamMembership(
                team_id=mapping.team_id,
                user_id=user.id,
                role=mapping.role,
                origin="ldap",
            )
            session.add(new_membership)
            existing_by_team[mapping.team_id] = new_membership
        elif existing.origin == "ldap":
            existing.role = mapping.role
        # existing.origin == "manual": leave untouched, per spec.

    # Remove ldap-origin memberships whose team is no longer matched.
    for team_id, membership in existing_by_team.items():
        if membership.origin == "ldap" and team_id not in matched_team_ids:
            await session.delete(membership)
