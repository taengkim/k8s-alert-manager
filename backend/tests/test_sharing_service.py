"""Tests for app.services.sharing: share_matches's reuse of the routing
engine's matcher semantics (include AND/re.search, exclude OR/re.search,
None/[] = everything), its dual AlertEvent/live-alert signature via
MatchableAlert, and shared_source_team_ids's single query.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.share import AlertShare
from app.models.team import Team
from app.services.sharing import (
    MatchableAlert,
    compile_share_scope,
    scope_matches,
    share_matches,
    shared_source_team_ids,
)


def _event(
    *,
    alertname: str = "HighCpu",
    severity: str | None = "critical",
    namespace: str | None = "kam-demo",
) -> AlertEvent:
    return AlertEvent(
        cluster_id=1,
        cluster_name="c1",
        fingerprint="fp",
        status="firing",
        alertname=alertname,
        severity=severity,
        namespace=namespace,
        labels={"alertname": alertname, "severity": severity or ""},
        annotations={},
        team_id=1,
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _share(matchers: list[dict] | None) -> AlertShare:
    return AlertShare(
        id=1, owner_team_id=1, target_team_id=2, mode="view_notify", matchers=matchers
    )


def test_share_matches_with_no_matchers_is_always_true() -> None:
    assert share_matches(_share(None), _event()) is True
    assert share_matches(_share([]), _event()) is True


def test_share_matches_include_matcher_on_label() -> None:
    share = _share(
        [{"kind": "include", "target": "label", "key": "severity", "pattern": "^critical$"}]
    )
    assert share_matches(share, _event(severity="critical")) is True
    assert share_matches(share, _event(severity="warning")) is False


def test_share_matches_multiple_include_matchers_are_and() -> None:
    share = _share(
        [
            {"kind": "include", "target": "label", "key": "severity", "pattern": "^critical$"},
            {"kind": "include", "target": "alertname", "key": None, "pattern": "^HighCpu$"},
        ]
    )
    # Both include matchers must pass.
    assert share_matches(share, _event(alertname="HighCpu", severity="critical")) is True
    # Severity fails -> whole thing fails even though alertname matches.
    assert share_matches(share, _event(alertname="HighCpu", severity="warning")) is False
    # Alertname fails -> whole thing fails even though severity matches.
    assert share_matches(share, _event(alertname="LowCpu", severity="critical")) is False


def test_share_matches_exclude_matcher_is_or_and_wins() -> None:
    share = _share(
        [
            {"kind": "include", "target": "alertname", "key": None, "pattern": ".*"},
            {"kind": "exclude", "target": "namespace_never_used", "key": None, "pattern": "x"},
            {"kind": "exclude", "target": "label", "key": "severity", "pattern": "^info$"},
        ]
    )
    assert share_matches(share, _event(severity="critical")) is True
    assert share_matches(share, _event(severity="info")) is False


def test_share_matches_invalid_matcher_pattern_fails_closed() -> None:
    """Unlike compile_rule (a routing rule fails *open* -- an uncompilable
    pattern just degrades that one condition to never-firing), a share's
    matchers gate a security boundary: if one fails to compile, the whole
    share must deny rather than silently drop the only restriction the
    owner configured and fall back to "matches everything".
    """
    share = _share(
        [{"kind": "include", "target": "alertname", "key": None, "pattern": "(unterminated"}]
    )
    assert share_matches(share, _event()) is False


def test_share_matches_malformed_matcher_dict_fails_closed() -> None:
    """A matcher dict missing a required key (kind/target/pattern) is
    dropped by build_transient_matchers with a warning rather than raising
    -- but, same as an uncompilable pattern, that must still deny the share
    rather than silently narrow its scope.
    """
    share = _share([{"kind": "include", "pattern": "x"}])  # missing "target"
    assert share_matches(share, _event()) is False


def test_share_matches_works_on_live_alert_dict_via_matchable_alert() -> None:
    """The same share, evaluated against a live Alertmanager alert (as
    flattened by app/api/alerts.py's _flatten) instead of an AlertEvent --
    both must use identical matcher semantics.
    """
    share = _share(
        [{"kind": "include", "target": "label", "key": "severity", "pattern": "^critical$"}]
    )
    live_alert = {
        "alertname": "HighCpu",
        "labels": {"alertname": "HighCpu", "severity": "critical"},
        "annotations": {},
    }
    assert share_matches(share, MatchableAlert.from_live_alert(live_alert)) is True

    live_alert["labels"]["severity"] = "warning"
    assert share_matches(share, MatchableAlert.from_live_alert(live_alert)) is False


def test_share_matches_live_alert_missing_keys_degrades_gracefully() -> None:
    share = _share(None)
    assert share_matches(share, MatchableAlert.from_live_alert({})) is True


def test_compile_share_scope_reused_across_many_scope_matches_calls() -> None:
    """The compile-once/check-many pattern app/api/alerts.py's live/history
    paths use: one compile_share_scope call, many scope_matches calls
    against it -- must agree with share_matches's one-shot equivalent for
    each alert.
    """
    share = _share(
        [{"kind": "include", "target": "label", "key": "severity", "pattern": "^critical$"}]
    )
    scope = compile_share_scope(share)

    critical = _event(severity="critical")
    warning = _event(severity="warning")
    assert scope_matches(scope, critical) is True
    assert scope_matches(scope, warning) is False
    # Same scope object reused for a second round -- no hidden per-call state.
    assert scope_matches(scope, critical) is True
    assert scope_matches(scope, warning) is False

    assert share_matches(share, critical) is True
    assert share_matches(share, warning) is False


def test_compile_share_scope_fail_closed_flag_is_precomputed() -> None:
    share = _share([{"kind": "include", "target": "alertname", "pattern": "(unterminated"}])
    scope = compile_share_scope(share)
    assert scope.always_denies is True
    assert scope_matches(scope, _event()) is False


async def _create_team(session: AsyncSession, slug: str) -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def test_shared_source_team_ids_returns_only_shares_targeting_viewer(app) -> None:
    async with db_module.async_session_factory() as session:
        platform = await _create_team(session, "platform")
        payments = await _create_team(session, "payments")
        other = await _create_team(session, "other")

        # platform -> payments (targets payments)
        session.add(
            AlertShare(owner_team_id=platform.id, target_team_id=payments.id, mode="view")
        )
        # other -> platform (does NOT target payments)
        session.add(AlertShare(owner_team_id=other.id, target_team_id=platform.id, mode="view"))
        await session.commit()

        pairs = await shared_source_team_ids(session, payments.id)
        assert [owner_id for owner_id, _ in pairs] == [platform.id]

        empty = await shared_source_team_ids(session, other.id)
        assert empty == []
