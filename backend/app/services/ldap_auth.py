"""LDAP authentication against the directory configured in Settings.

ldap3 calls are wrapped in small module-level functions so tests can
monkeypatch them without a live LDAP server.
"""

from dataclasses import dataclass

from ldap3 import ALL_ATTRIBUTES, Connection, Server
from ldap3.core.exceptions import LDAPAttributeError, LDAPExceptionError

from app.config import get_settings


@dataclass
class LdapUserInfo:
    dn: str
    username: str
    display_name: str
    email: str | None
    group_dns: list[str]


def _service_connection() -> Connection:
    """Bind as the service account. Split out for testability."""
    settings = get_settings()
    server = Server(settings.ldap_url)
    return Connection(
        server, settings.ldap_bind_dn, settings.ldap_bind_password, auto_bind=True
    )


def _bind_as_user(dn: str, password: str) -> bool:
    """Attempt to bind as the given user DN with the supplied password."""
    settings = get_settings()
    server = Server(settings.ldap_url)
    try:
        conn = Connection(server, dn, password, auto_bind=True)
    except LDAPExceptionError:
        return False
    conn.unbind()
    return True


def _search_user(conn: Connection, username: str):
    settings = get_settings()
    search_filter = settings.ldap_user_filter.format(username=username)
    try:
        conn.search(
            settings.ldap_user_base,
            search_filter,
            attributes=["cn", "displayName", "mail", "memberOf"],
        )
    except LDAPAttributeError:
        # Plain OpenLDAP without the memberof overlay doesn't recognize
        # memberOf as a valid attribute at all (schema-checking rejects the
        # search outright, it doesn't just come back empty). Retry without
        # it and rely on the groupOfNames fallback search for group_dns.
        conn.search(
            settings.ldap_user_base,
            search_filter,
            attributes=["cn", "displayName", "mail"],
        )
    if not conn.entries:
        return None
    return conn.entries[0]


def _search_group_dns(conn: Connection, user_dn: str) -> list[str]:
    """Fallback lookup: find groups (groupOfNames) whose member is user_dn."""
    settings = get_settings()
    conn.search(
        settings.ldap_group_base,
        f"(member={user_dn})",
        attributes=[ALL_ATTRIBUTES],
    )
    return [entry.entry_dn for entry in conn.entries]


def authenticate(username: str, password: str) -> LdapUserInfo | None:
    conn = _service_connection()
    try:
        entry = _search_user(conn, username)
        if entry is None:
            return None

        user_dn = entry.entry_dn

        if not _bind_as_user(user_dn, password):
            return None

        display_name = _first_attr(entry, "displayName") or _first_attr(entry, "cn") or username
        email = _first_attr(entry, "mail")

        member_of = _attr_values(entry, "memberOf")
        if member_of:
            group_dns = member_of
        else:
            group_dns = _search_group_dns(conn, user_dn)

        return LdapUserInfo(
            dn=user_dn,
            username=username,
            display_name=display_name,
            email=email,
            group_dns=group_dns,
        )
    finally:
        conn.unbind()


def _attr_values(entry, name: str) -> list[str]:
    if name not in entry.entry_attributes:
        return []
    value = entry[name].values
    return list(value) if value else []


def _first_attr(entry, name: str) -> str | None:
    values = _attr_values(entry, name)
    return values[0] if values else None
