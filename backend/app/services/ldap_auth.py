"""LDAP authentication against the directory configured in Settings.

ldap3 calls are wrapped in small module-level functions so tests can
monkeypatch them without a live LDAP server.
"""

from dataclasses import dataclass

from ldap3 import ALL_ATTRIBUTES, Connection, Server
from ldap3.core.exceptions import LDAPAttributeError, LDAPExceptionError
from ldap3.utils.conv import escape_filter_chars

from app.config import get_settings


@dataclass
class LdapUserInfo:
    dn: str
    username: str
    display_name: str
    email: str | None
    group_dns: list[str]


class LdapUnavailableError(Exception):
    """Raised when the LDAP directory itself can't be reached/bound as the
    service account -- distinct from "wrong credentials", which is reported
    as `authenticate() -> None`.
    """


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
    # Escape LDAP filter metacharacters in the caller-supplied username so a
    # value like "*" or "admin)(uid=*" can't widen or corrupt the filter
    # (LDAP filter injection).
    safe_username = escape_filter_chars(username)
    search_filter = settings.ldap_user_filter.format(username=safe_username)
    try:
        conn.search(
            settings.ldap_user_base,
            search_filter,
            attributes=["uid", "cn", "displayName", "mail", "memberOf"],
        )
    except LDAPAttributeError:
        # Plain OpenLDAP without the memberof overlay doesn't recognize
        # memberOf as a valid attribute at all (schema-checking rejects the
        # search outright, it doesn't just come back empty). Retry without
        # it and rely on the groupOfNames fallback search for group_dns.
        conn.search(
            settings.ldap_user_base,
            search_filter,
            attributes=["uid", "cn", "displayName", "mail"],
        )
    # Exactly one match expected; a filter that (despite escaping) matches
    # zero or more than one entry is treated as a failed lookup rather than
    # picking entries[0] and authenticating against the wrong record.
    if len(conn.entries) != 1:
        return None
    return conn.entries[0]


def _search_group_dns(conn: Connection, user_dn: str) -> list[str]:
    """Fallback lookup: find groups (groupOfNames) whose member is user_dn."""
    settings = get_settings()
    safe_user_dn = escape_filter_chars(user_dn)
    conn.search(
        settings.ldap_group_base,
        f"(member={safe_user_dn})",
        attributes=[ALL_ATTRIBUTES],
    )
    return [entry.entry_dn for entry in conn.entries]


def _rdn_value(dn: str) -> str | None:
    """Best-effort extraction of the leftmost RDN's value, e.g.
    'uid=alice,ou=users,dc=example,dc=org' -> 'alice'. Used as a fallback
    canonical username when the entry has no `uid` attribute.
    """
    first_component = dn.split(",", 1)[0]
    if "=" not in first_component:
        return None
    return first_component.split("=", 1)[1].strip() or None


def authenticate(username: str, password: str) -> LdapUserInfo | None:
    # ldap3 performs a SIMPLE bind with an empty password as RFC 4513
    # "unauthenticated authentication", which many directories accept as a
    # successful bind. Reject up front so no caller downstream can be
    # tricked into treating an empty/blank password as a valid login.
    if not password or not password.strip():
        return None

    try:
        conn = _service_connection()
    except LDAPExceptionError as exc:
        raise LdapUnavailableError("could not bind to LDAP as the service account") from exc

    try:
        entry = _search_user(conn, username)
        if entry is None:
            return None

        user_dn = entry.entry_dn

        if not _bind_as_user(user_dn, password):
            return None

        display_name = _first_attr(entry, "displayName") or _first_attr(entry, "cn") or username
        email = _first_attr(entry, "mail")

        # Canonical username from the directory, never the raw request
        # string: LDAP uid matching is case-insensitive, so without this a
        # login as "ALICE" would create a second, distinct User row and
        # sidestep is_active/is_admin state already set on "alice".
        canonical_username = _first_attr(entry, "uid") or _rdn_value(user_dn) or username

        member_of = _attr_values(entry, "memberOf")
        if member_of:
            group_dns = member_of
        else:
            group_dns = _search_group_dns(conn, user_dn)

        return LdapUserInfo(
            dn=user_dn,
            username=canonical_username,
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
