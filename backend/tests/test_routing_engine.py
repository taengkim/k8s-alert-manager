"""Pure-function tests for app/services/routing.py's evaluation engine:
`compile_rule` and `evaluate`. No database, no API -- every object here is a
transient (never-session-added) ORM instance, since evaluate() only reads
attributes off them.
"""

from datetime import UTC, datetime

import pytest

from app.models.alert import AlertEvent
from app.models.routing import RoutingMatcher, RoutingRule
from app.services.routing import VerdictKind, compile_rule, evaluate, evaluate_compiled


def _event(
    *,
    cluster_id: int = 1,
    status: str = "firing",
    alertname: str = "HighCpu",
    severity: str | None = "critical",
    namespace: str | None = "kam-demo",
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> AlertEvent:
    return AlertEvent(
        id=1,
        cluster_id=cluster_id,
        cluster_name="local",
        fingerprint="fp",
        status=status,
        alertname=alertname,
        severity=severity,
        namespace=namespace,
        labels=labels if labels is not None else {"alertname": alertname},
        annotations=annotations or {},
        team_id=1,
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _rule(
    *,
    action: str = "notify",
    enabled: bool = True,
    notify_on_firing: bool = True,
    notify_on_resolved: bool = False,
    severities: list[str] | None = None,
    namespaces_include: list[str] | None = None,
    namespaces_exclude: list[str] | None = None,
    clusters: list[int] | None = None,
) -> RoutingRule:
    return RoutingRule(
        id=1,
        team_id=1,
        name="r",
        action=action,
        enabled=enabled,
        notify_on_firing=notify_on_firing,
        notify_on_resolved=notify_on_resolved,
        severities=severities,
        namespaces_include=namespaces_include,
        namespaces_exclude=namespaces_exclude,
        clusters=clusters,
    )


def _matcher(
    *, kind: str, target: str, pattern: str, key: str | None = None, position: int = 0
) -> RoutingMatcher:
    return RoutingMatcher(kind=kind, target=target, key=key, pattern=pattern, position=position)


def test_matched_when_nothing_filters() -> None:
    assert evaluate(_event(), _rule(), []).kind is VerdictKind.MATCHED


# --- step 0: clusters ---------------------------------------------------


def test_cluster_filter_blocks_other_clusters() -> None:
    verdict = evaluate(_event(cluster_id=2), _rule(clusters=[1]), [])
    assert verdict.kind is VerdictKind.CLUSTER_FILTERED


def test_cluster_filter_passes_when_included() -> None:
    verdict = evaluate(_event(cluster_id=1), _rule(clusters=[1, 2]), [])
    assert verdict.kind is VerdictKind.MATCHED


def test_cluster_filter_null_means_no_filter() -> None:
    verdict = evaluate(_event(cluster_id=999), _rule(clusters=None), [])
    assert verdict.kind is VerdictKind.MATCHED


# --- step 1: gate ---------------------------------------------------------


def test_disabled_rule_is_gated() -> None:
    verdict = evaluate(_event(), _rule(enabled=False), [])
    assert verdict.kind is VerdictKind.GATED


def test_notify_rule_gated_on_firing_when_notify_on_firing_false() -> None:
    verdict = evaluate(
        _event(status="firing"), _rule(notify_on_firing=False, notify_on_resolved=True), []
    )
    assert verdict.kind is VerdictKind.GATED


def test_notify_rule_gated_on_resolved_when_notify_on_resolved_false() -> None:
    verdict = evaluate(
        _event(status="resolved"), _rule(notify_on_firing=True, notify_on_resolved=False), []
    )
    assert verdict.kind is VerdictKind.GATED


def test_notify_rule_passes_gate_when_trigger_enabled() -> None:
    verdict = evaluate(_event(status="resolved"), _rule(notify_on_resolved=True), [])
    assert verdict.kind is VerdictKind.MATCHED


def test_suppress_rule_ignores_notify_on_gate_for_firing() -> None:
    # notify_on_firing/resolved both False -- a notify rule would be gated,
    # but a suppress rule always evaluates regardless of trigger.
    rule = _rule(action="suppress", notify_on_firing=False, notify_on_resolved=False)
    assert evaluate(_event(status="firing"), rule, []).kind is VerdictKind.MATCHED
    assert evaluate(_event(status="resolved"), rule, []).kind is VerdictKind.MATCHED


def test_suppress_rule_still_gated_when_disabled() -> None:
    rule = _rule(action="suppress", enabled=False)
    assert evaluate(_event(), rule, []).kind is VerdictKind.GATED


def test_trigger_override_wins_over_event_status() -> None:
    # event.status="resolved" would gate this rule (notify_on_resolved
    # defaults to False) if the gate read event.status directly -- an
    # explicit trigger override must take precedence, since that's the
    # whole point of the parameter (preview evaluating "as if firing"
    # against events whose stored status may since have changed).
    rule = _rule(notify_on_firing=True, notify_on_resolved=False)
    event = _event(status="resolved")
    assert evaluate(event, rule, [], trigger="firing").kind is VerdictKind.MATCHED
    assert evaluate(event, rule, [], trigger="resolved").kind is VerdictKind.GATED
    # Omitted -> falls back to event.status, same as before this param existed.
    assert evaluate(event, rule, []).kind is VerdictKind.GATED


def test_evaluate_compiled_matches_evaluate_for_the_same_inputs() -> None:
    rule = _rule(severities=["critical"])
    matchers = [_matcher(kind="include", target="alertname", pattern="Cpu")]
    event = _event(alertname="HighCpuUsage", severity="critical")

    compiled = compile_rule(rule, matchers)
    assert evaluate_compiled(event, compiled).kind is VerdictKind.MATCHED
    assert evaluate(event, rule, matchers).kind is VerdictKind.MATCHED


# --- step 2: severities -----------------------------------------------------


@pytest.mark.parametrize(
    ("event_severity", "rule_severities", "expected"),
    [
        ("critical", None, VerdictKind.MATCHED),
        ("critical", [], VerdictKind.MATCHED),
        ("critical", ["critical", "warning"], VerdictKind.MATCHED),
        ("Critical", ["critical"], VerdictKind.MATCHED),  # case-insensitive
        ("info", ["critical", "warning"], VerdictKind.SEVERITY_FILTERED),
        (None, ["none"], VerdictKind.MATCHED),
        (None, ["critical"], VerdictKind.SEVERITY_FILTERED),
        (None, None, VerdictKind.MATCHED),
    ],
)
def test_severity_matrix(event_severity, rule_severities, expected) -> None:
    verdict = evaluate(_event(severity=event_severity), _rule(severities=rule_severities), [])
    assert verdict.kind is expected


# --- step 3/4: namespaces --------------------------------------------------


def test_namespace_include_null_passes_regardless_of_namespace() -> None:
    verdict = evaluate(_event(namespace=None), _rule(namespaces_include=None), [])
    assert verdict.kind is VerdictKind.MATCHED


def test_namespace_include_anchored_fullmatch() -> None:
    # "kam" would `search`-match "kam-demo" but must NOT `fullmatch` it.
    verdict = evaluate(_event(namespace="kam-demo"), _rule(namespaces_include=["kam"]), [])
    assert verdict.kind is VerdictKind.NAMESPACE_FILTERED

    verdict_ok = evaluate(
        _event(namespace="kam-demo"), _rule(namespaces_include=["kam-.*"]), []
    )
    assert verdict_ok.kind is VerdictKind.MATCHED


def test_namespace_include_null_namespace_fails_when_include_nonempty() -> None:
    verdict = evaluate(_event(namespace=None), _rule(namespaces_include=["kam-.*"]), [])
    assert verdict.kind is VerdictKind.NAMESPACE_FILTERED


def test_namespace_exclude_blocks_on_fullmatch() -> None:
    verdict = evaluate(_event(namespace="kam-demo"), _rule(namespaces_exclude=["kam-.*"]), [])
    assert verdict.kind is VerdictKind.NAMESPACE_FILTERED


def test_namespace_exclude_null_namespace_never_excluded() -> None:
    verdict = evaluate(_event(namespace=None), _rule(namespaces_exclude=["kam-.*"]), [])
    assert verdict.kind is VerdictKind.MATCHED


# --- step 5/6: matchers -----------------------------------------------------


def test_include_matcher_alertname_search() -> None:
    matcher = _matcher(kind="include", target="alertname", pattern="Cpu")
    assert evaluate(_event(alertname="HighCpuUsage"), _rule(), [matcher]).kind is VerdictKind.MATCHED
    verdict = evaluate(_event(alertname="HighMemUsage"), _rule(), [matcher])
    assert verdict.kind is VerdictKind.NOT_INCLUDED
    assert verdict.blocking_matcher_position == 0


def test_include_matchers_are_anded() -> None:
    matchers = [
        _matcher(kind="include", target="alertname", pattern="Cpu", position=0),
        _matcher(kind="include", target="label", key="team", pattern="platform", position=1),
    ]
    event_ok = _event(labels={"team": "platform"})
    assert evaluate(event_ok, _rule(), matchers).kind is VerdictKind.MATCHED

    event_bad = _event(labels={"team": "other"})
    verdict = evaluate(event_bad, _rule(), matchers)
    assert verdict.kind is VerdictKind.NOT_INCLUDED
    assert verdict.blocking_matcher_position == 1


def test_include_matcher_missing_key_never_matches() -> None:
    matcher = _matcher(kind="include", target="label", key="team", pattern=".*")
    verdict = evaluate(_event(labels={}), _rule(), [matcher])
    assert verdict.kind is VerdictKind.NOT_INCLUDED


def test_include_matcher_annotation_target() -> None:
    matcher = _matcher(kind="include", target="annotation", key="runbook_url", pattern="runbooks")
    event = _event(annotations={"runbook_url": "https://runbooks.example.com/x"})
    assert evaluate(event, _rule(), [matcher]).kind is VerdictKind.MATCHED


def test_exclude_matchers_are_ored() -> None:
    matchers = [
        _matcher(kind="exclude", target="alertname", pattern="Noisy", position=0),
        _matcher(kind="exclude", target="label", key="stage", pattern="dev", position=1),
    ]
    assert evaluate(_event(alertname="Fine", labels={}), _rule(), matchers).kind is VerdictKind.MATCHED

    verdict = evaluate(_event(alertname="Fine", labels={"stage": "dev"}), _rule(), matchers)
    assert verdict.kind is VerdictKind.EXCLUDED
    assert verdict.blocking_matcher_position == 1


def test_include_wins_evaluation_order_over_exclude() -> None:
    # NOT_INCLUDED must be reported even though there's also a would-be
    # exclude match -- include (step 5) runs before exclude (step 6).
    matchers = [
        _matcher(kind="include", target="alertname", pattern="Cpu", position=0),
        _matcher(kind="exclude", target="alertname", pattern="High", position=1),
    ]
    verdict = evaluate(_event(alertname="HighMemUsage"), _rule(), matchers)
    assert verdict.kind is VerdictKind.NOT_INCLUDED


# --- suppress beats notify is a route_event-level property, but the engine
# itself must treat a suppress rule exactly the same as a notify rule for
# every filter step besides the notify_on gate. ------------------------------


def test_suppress_rule_matched_like_notify_rule() -> None:
    rule = _rule(action="suppress", severities=["critical"])
    assert evaluate(_event(severity="critical"), rule, []).kind is VerdictKind.MATCHED
    assert evaluate(_event(severity="info"), rule, []).kind is VerdictKind.SEVERITY_FILTERED


# --- defensive regex compilation --------------------------------------------


def test_compile_rule_skips_invalid_matcher_pattern_without_raising() -> None:
    bad = _matcher(kind="include", target="alertname", pattern="(unterminated")
    compiled = compile_rule(_rule(), [bad])
    assert compiled.include_matchers == ()


def test_compile_rule_skips_invalid_namespace_pattern_without_raising() -> None:
    compiled = compile_rule(_rule(namespaces_include=["(unterminated", "valid-.*"]), [])
    assert len(compiled.namespaces_include) == 1


def test_evaluate_with_invalid_matcher_pattern_does_not_raise() -> None:
    bad = _matcher(kind="include", target="alertname", pattern="(unterminated")
    # The one include matcher is dropped defensively, so with none left the
    # step 5 AND is vacuously true -- the rule matches.
    verdict = evaluate(_event(), _rule(), [bad])
    assert verdict.kind is VerdictKind.MATCHED
