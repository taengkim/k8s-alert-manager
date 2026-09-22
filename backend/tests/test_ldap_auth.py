"""Unit tests for app.services.ldap_auth against monkeypatched internal
seams (_service_connection, _bind_as_user, _search_user,
_search_group_dns) -- no live LDAP server required.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from ldap3.core.exceptions import LDAPAttributeError, LDAPSocketOpenError

from app.services import ldap_auth


class FakeEntry:
    """Minimal stand-in for an ldap3 Entry, supporting the
    entry_attributes / __getitem__(...).values / entry_dn protocol that
    _attr_values/_first_attr rely on.
    """

    def __init__(self, attrs: dict[str, list[str]], entry_dn: str):
        self._attrs = attrs
        self.entry_dn = entry_dn

    @property
    def entry_attributes(self) -> list[str]:
        return list(self._attrs.keys())

    def __getitem__(self, name: str) -> SimpleNamespace:
        return SimpleNamespace(values=self._attrs.get(name, []))


class FakeConnection:
    """A connection stand-in whose .search() just installs canned entries,
    for tests that don't care about the filter/attributes passed in.
    """

    def __init__(self, entries: list[FakeEntry] | None = None):
        self.entries = entries or []

    def search(self, base, search_filter, attributes=None):
        pass

    def unbind(self):
        pass


class RecordingConnection(FakeConnection):
    """Records every .search() call's filter/attributes for assertions."""

    def __init__(self, entries: list[FakeEntry] | None = None):
        super().__init__(entries)
        self.calls: list[dict] = []

    def search(self, base, search_filter, attributes=None):
        self.calls.append({"base": base, "filter": search_filter, "attributes": attributes})


class FlakyMemberOfConnection(FakeConnection):
    """Raises LDAPAttributeError the first time memberOf is requested (as a
    directory without the memberof overlay would), then succeeds on retry.
    """

    def __init__(self, entries: list[FakeEntry]):
        super().__init__()
        self._final_entries = entries
        self.calls: list[list[str]] = []

    def search(self, base, search_filter, attributes=None):
        attrs = list(attributes or [])
        self.calls.append(attrs)
        if "memberOf" in attrs:
            raise LDAPAttributeError("invalid attribute type memberOf")
        self.entries = self._final_entries


ALICE_DN = "uid=alice,ou=users,dc=example,dc=org"


def _alice_entry(**extra_attrs: list[str]) -> FakeEntry:
    attrs = {"uid": ["alice"], "cn": ["Alice Kim"], "mail": ["alice@example.org"]}
    attrs.update(extra_attrs)
    return FakeEntry(attrs, ALICE_DN)


# --- (a) re-bind uses the found DN + supplied password; failure -> None ---


def test_authenticate_binds_as_the_found_user_dn_with_supplied_password(monkeypatch):
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: FakeConnection())
    monkeypatch.setattr(ldap_auth, "_search_user", lambda conn, username: _alice_entry())
    bind_mock = Mock(return_value=True)
    monkeypatch.setattr(ldap_auth, "_bind_as_user", bind_mock)

    result = ldap_auth.authenticate("alice", "correct horse")

    bind_mock.assert_called_once_with(ALICE_DN, "correct horse")
    assert result is not None
    assert result.dn == ALICE_DN


def test_authenticate_returns_none_when_rebind_fails(monkeypatch):
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: FakeConnection())
    monkeypatch.setattr(ldap_auth, "_search_user", lambda conn, username: _alice_entry())
    monkeypatch.setattr(ldap_auth, "_bind_as_user", Mock(return_value=False))

    assert ldap_auth.authenticate("alice", "wrong") is None


# --- (b) unknown user -> None, without ever attempting a user bind ---


def test_authenticate_unknown_user_returns_none_without_binding(monkeypatch):
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: FakeConnection())
    monkeypatch.setattr(ldap_auth, "_search_user", lambda conn, username: None)
    bind_mock = Mock(return_value=True)
    monkeypatch.setattr(ldap_auth, "_bind_as_user", bind_mock)

    assert ldap_auth.authenticate("nosuchuser", "whatever") is None
    bind_mock.assert_not_called()


# --- (c) memberOf used when present, groupOfNames fallback when absent ---


def test_authenticate_uses_memberof_when_present(monkeypatch):
    entry = _alice_entry(memberOf=["cn=team-platform,ou=groups,dc=example,dc=org"])
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: FakeConnection())
    monkeypatch.setattr(ldap_auth, "_search_user", lambda conn, username: entry)
    monkeypatch.setattr(ldap_auth, "_bind_as_user", lambda dn, pw: True)
    fallback_mock = Mock(return_value=["should-not-be-used"])
    monkeypatch.setattr(ldap_auth, "_search_group_dns", fallback_mock)

    info = ldap_auth.authenticate("alice", "password")

    fallback_mock.assert_not_called()
    assert info.group_dns == ["cn=team-platform,ou=groups,dc=example,dc=org"]


def test_authenticate_falls_back_to_group_search_when_memberof_absent(monkeypatch):
    entry = _alice_entry()  # no memberOf attribute at all
    conn = FakeConnection()
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: conn)
    monkeypatch.setattr(ldap_auth, "_search_user", lambda conn, username: entry)
    monkeypatch.setattr(ldap_auth, "_bind_as_user", lambda dn, pw: True)
    fallback_mock = Mock(return_value=["cn=team-platform,ou=groups,dc=example,dc=org"])
    monkeypatch.setattr(ldap_auth, "_search_group_dns", fallback_mock)

    info = ldap_auth.authenticate("alice", "password")

    fallback_mock.assert_called_once_with(conn, ALICE_DN)
    assert info.group_dns == ["cn=team-platform,ou=groups,dc=example,dc=org"]


# --- (d) LDAPAttributeError retry path (memberof-overlay-missing directory) ---


def test_search_user_retries_without_memberof_on_attribute_error():
    entry = _alice_entry()
    conn = FlakyMemberOfConnection([entry])

    result = ldap_auth._search_user(conn, "alice")

    assert result is entry
    assert conn.calls[0] == ["uid", "cn", "displayName", "mail", "memberOf"]
    assert conn.calls[1] == ["uid", "cn", "displayName", "mail"]


# --- (e) C1: empty/blank password rejected before any LDAP call ---


def test_authenticate_rejects_empty_password_without_any_ldap_call(monkeypatch):
    service_mock = Mock(side_effect=AssertionError("must not be called"))
    monkeypatch.setattr(ldap_auth, "_service_connection", service_mock)

    assert ldap_auth.authenticate("alice", "") is None
    assert ldap_auth.authenticate("alice", "   ") is None
    service_mock.assert_not_called()


# --- (e) C3: LDAP filter injection is escaped, and non-unique matches fail ---


def test_search_user_escapes_filter_special_characters():
    conn = RecordingConnection(entries=[])
    ldap_auth._search_user(conn, "alice)(uid=*")

    sent_filter = conn.calls[0]["filter"]
    assert ")(uid=*" not in sent_filter
    assert "\\29" in sent_filter  # escaped ')'
    assert "\\28" in sent_filter  # escaped '('
    assert "\\2a" in sent_filter  # escaped '*'


def test_search_user_rejects_zero_or_multiple_matches():
    conn = RecordingConnection(entries=[])
    assert ldap_auth._search_user(conn, "nobody") is None

    conn2 = RecordingConnection(entries=[_alice_entry(), _alice_entry()])
    assert ldap_auth._search_user(conn2, "alice") is None


def test_search_group_dns_escapes_user_dn():
    conn = RecordingConnection(entries=[])
    ldap_auth._search_group_dns(conn, "uid=weird)(uid=*,ou=users,dc=example,dc=org")

    sent_filter = conn.calls[0]["filter"]
    assert ")(uid=*" not in sent_filter
    assert "\\29" in sent_filter


# --- I2: a service-bind failure raises LdapUnavailableError, not a raw 500 ---


def test_authenticate_raises_unavailable_when_service_bind_fails(monkeypatch):
    def boom():
        raise LDAPSocketOpenError("cannot connect to directory")

    monkeypatch.setattr(ldap_auth, "_service_connection", boom)

    with pytest.raises(ldap_auth.LdapUnavailableError):
        ldap_auth.authenticate("alice", "password")


# --- C2: canonical username comes from the directory, not the request ---


def test_authenticate_uses_directory_uid_as_canonical_username(monkeypatch):
    entry = _alice_entry()  # uid=alice regardless of requested casing
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: FakeConnection())
    monkeypatch.setattr(ldap_auth, "_search_user", lambda conn, username: entry)
    monkeypatch.setattr(ldap_auth, "_bind_as_user", lambda dn, pw: True)
    monkeypatch.setattr(ldap_auth, "_search_group_dns", lambda conn, dn: [])

    info = ldap_auth.authenticate("ALICE", "password")

    assert info.username == "alice"


def test_authenticate_falls_back_to_rdn_value_when_uid_attribute_missing(monkeypatch):
    entry = FakeEntry({"cn": ["Alice Kim"]}, ALICE_DN)  # no uid attribute
    monkeypatch.setattr(ldap_auth, "_service_connection", lambda: FakeConnection())
    monkeypatch.setattr(ldap_auth, "_search_user", lambda conn, username: entry)
    monkeypatch.setattr(ldap_auth, "_bind_as_user", lambda dn, pw: True)
    monkeypatch.setattr(ldap_auth, "_search_group_dns", lambda conn, dn: [])

    info = ldap_auth.authenticate("ALICE", "password")

    assert info.username == "alice"  # extracted from the RDN of entry_dn
