import re

import pytest
from pydantic import ValidationError

from app.models.team import Team
from app.services.rules import (
    BUILDER_STATE_ANNOTATION,
    RULE_SLUG_RE,
    BuilderLabelFilter,
    BuilderState,
    RuleWrite,
    build_prometheus_rule,
    generate_builder_expr,
    parse_prometheus_rule,
    rule_object_name,
)


def _team(team_id: int = 1, slug: str = "platform") -> Team:
    return Team(id=team_id, slug=slug, name=slug.title())


def test_build_sets_ownership_labels_and_name() -> None:
    team = _team()
    rule_input = RuleWrite(
        slug="high-cpu",
        alert_name="HighCpu",
        expr="cpu_usage > 0.9",
        severity="warning",
    )

    manifest = build_prometheus_rule(team, rule_input)

    assert manifest["metadata"]["name"] == "kam-t1-high-cpu"
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "kam",
        "kam/team-id": "1",
        "kam/team-slug": "platform",
    }
    assert manifest["spec"]["groups"][0]["name"] == "kam-platform"


def test_object_names_are_keyed_by_team_id_not_slug() -> None:
    """Regression test for a naming collision at a team-slug hyphen
    boundary: under slug-based naming, team "web" rule "api-latency" and
    team "web-api" rule "latency" would both stringify to the same object
    name ("kam-web-api-latency"). Numeric team ids can't collide like that.
    """
    team_web = _team(team_id=1, slug="web")
    team_web_api = _team(team_id=2, slug="web-api")

    name_a = build_prometheus_rule(
        team_web,
        RuleWrite(slug="api-latency", alert_name="A", expr="up", severity="info"),
    )["metadata"]["name"]
    name_b = build_prometheus_rule(
        team_web_api,
        RuleWrite(slug="latency", alert_name="B", expr="up", severity="info"),
    )["metadata"]["name"]

    assert name_a == "kam-t1-api-latency"
    assert name_b == "kam-t2-latency"
    assert name_a != name_b


def test_rule_object_name_matches_build_prometheus_rule() -> None:
    assert rule_object_name(7, "my-slug") == "kam-t7-my-slug"


@pytest.mark.parametrize(
    "slug",
    ["x", "a1", "high-cpu", "a" * 63, "a-b-c"],
)
def test_slug_pattern_accepts_valid_slugs(slug: str) -> None:
    assert re.match(RULE_SLUG_RE, slug)


@pytest.mark.parametrize(
    "slug",
    ["-leading-hyphen", "trailing-hyphen-", "Uppercase", "under_score", "a" * 64, ""],
)
def test_slug_pattern_rejects_invalid_slugs(slug: str) -> None:
    assert not re.match(RULE_SLUG_RE, slug)


def test_build_forces_kam_team_and_severity_labels_over_user_supplied() -> None:
    team = _team(slug="payments")
    rule_input = RuleWrite(
        slug="lag",
        alert_name="QueueLag",
        expr="queue_lag > 100",
        severity="critical",
        # A user could try to smuggle in their own kam_team/severity; the
        # forced values must win.
        labels={"kam_team": "attacker", "severity": "info", "team_owner": "sre"},
    )

    manifest = build_prometheus_rule(team, rule_input)
    rule = manifest["spec"]["groups"][0]["rules"][0]

    assert rule["labels"]["kam_team"] == "payments"
    assert rule["labels"]["severity"] == "critical"
    assert rule["labels"]["team_owner"] == "sre"


def test_build_lifts_runbook_and_grafana_url_into_annotations() -> None:
    team = _team()
    rule_input = RuleWrite(
        slug="disk-full",
        alert_name="DiskFull",
        expr="disk_free < 0.1",
        severity="critical",
        annotations={"summary": "disk almost full"},
        runbook_url="https://runbooks.example.com/disk-full",
        grafana_url="https://grafana.example.com/d/disk",
    )

    manifest = build_prometheus_rule(team, rule_input)
    rule = manifest["spec"]["groups"][0]["rules"][0]

    assert rule["annotations"]["summary"] == "disk almost full"
    assert rule["annotations"]["runbook_url"] == "https://runbooks.example.com/disk-full"
    assert rule["annotations"]["kam_grafana_url"] == "https://grafana.example.com/d/disk"


def test_build_omits_for_and_annotations_when_absent() -> None:
    team = _team()
    rule_input = RuleWrite(
        slug="always-firing",
        alert_name="AlwaysFiring",
        expr="vector(1)",
        severity="info",
    )

    manifest = build_prometheus_rule(team, rule_input)
    rule = manifest["spec"]["groups"][0]["rules"][0]

    assert "for" not in rule
    assert "annotations" not in rule


def test_build_includes_for_when_given() -> None:
    team = _team()
    rule_input = RuleWrite(
        slug="slow-burn",
        alert_name="SlowBurn",
        expr="rate(errors[5m]) > 0.01",
        for_="5m",
        severity="warning",
    )

    manifest = build_prometheus_rule(team, rule_input)
    rule = manifest["spec"]["groups"][0]["rules"][0]

    assert rule["for"] == "5m"


def test_rule_write_accepts_for_alias_from_json_body() -> None:
    # The frontend sends {"for": "5m", ...} -- populate_by_name plus the
    # "for" alias must accept that JSON key into the for_ field.
    rule_input = RuleWrite.model_validate(
        {
            "slug": "x",
            "alert_name": "X",
            "expr": "vector(1)",
            "for": "2m",
            "severity": "info",
        }
    )
    assert rule_input.for_ == "2m"


def test_parse_is_inverse_of_build_round_trip() -> None:
    team = _team(team_id=2, slug="platform")
    rule_input = RuleWrite(
        slug="high-cpu",
        alert_name="HighCpu",
        expr="cpu_usage > 0.9",
        for_="10m",
        severity="warning",
        labels={"team_owner": "sre"},
        annotations={"summary": "cpu is high"},
        runbook_url="https://runbooks.example.com/high-cpu",
        grafana_url="https://grafana.example.com/d/cpu",
    )

    manifest = build_prometheus_rule(team, rule_input)
    parsed = parse_prometheus_rule(manifest)

    assert parsed == {
        "slug": "high-cpu",
        "alert_name": "HighCpu",
        "expr": "cpu_usage > 0.9",
        "for": "10m",
        "severity": "warning",
        "labels": {"team_owner": "sre"},
        "annotations": {"summary": "cpu is high"},
        "runbook_url": "https://runbooks.example.com/high-cpu",
        "grafana_url": "https://grafana.example.com/d/cpu",
        "mode": "promql",
        "builder_state": None,
    }


def test_parse_strips_kam_prefix_using_the_objects_own_team_id_label() -> None:
    obj = {
        "metadata": {
            "name": "kam-t1-always-firing",
            "labels": {"app.kubernetes.io/managed-by": "kam", "kam/team-id": "1"},
        },
        "spec": {
            "groups": [
                {
                    "name": "kam-platform",
                    "rules": [
                        {
                            "alert": "KamAlwaysFiring",
                            "expr": "vector(1)",
                            "labels": {"kam_team": "platform", "severity": "info"},
                        }
                    ],
                }
            ]
        },
    }

    parsed = parse_prometheus_rule(obj)
    assert parsed["slug"] == "always-firing"


def test_parse_does_not_strip_prefix_when_team_id_label_is_missing() -> None:
    # Even if the name happens to look like "kam-t1-...", without the
    # metadata team-id label to confirm it, the prefix must not be stripped.
    obj = {
        "metadata": {"name": "kam-t1-always-firing"},
        "spec": {
            "groups": [
                {
                    "name": "g",
                    "rules": [{"alert": "KamAlwaysFiring", "expr": "vector(1)"}],
                }
            ]
        },
    }

    parsed = parse_prometheus_rule(obj)
    assert parsed["slug"] == "kam-t1-always-firing"


def test_parse_falls_back_to_full_name_when_no_kam_team_label() -> None:
    # A hand-crafted or foreign rule (no kam/team-id metadata label at all)
    # shouldn't crash the parser -- it just can't strip a prefix it can't
    # compute.
    obj = {
        "metadata": {"name": "some-foreign-rule"},
        "spec": {"groups": [{"name": "g", "rules": [{"alert": "X", "expr": "up"}]}]},
    }

    parsed = parse_prometheus_rule(obj)
    assert parsed["slug"] == "some-foreign-rule"
    assert parsed["severity"] == ""


# -- threshold builder: canonical expr generation --------------------------
#
# This format is pinned deliberately: the frontend's ThresholdBuilder.tsx
# generator must produce byte-for-byte the same string, since
# parse_prometheus_rule's mode detection compares this function's output
# against whatever expr the frontend actually stored.


def test_generate_builder_expr_no_labels() -> None:
    state = BuilderState(metric="node_load1", comparison=">", threshold=0)
    assert generate_builder_expr(state) == "node_load1 > 0"


def test_generate_builder_expr_with_labels_no_spaces_inside_braces() -> None:
    state = BuilderState(
        metric="node_load1",
        labels=[
            BuilderLabelFilter(key="job", op="=", value="node-exporter"),
            BuilderLabelFilter(key="instance", op="!~", value="test.*"),
        ],
        comparison=">=",
        threshold=1.5,
    )
    assert (
        generate_builder_expr(state)
        == 'node_load1{job="node-exporter",instance!~"test.*"} >= 1.5'
    )


def test_generate_builder_expr_integral_threshold_has_no_trailing_zero() -> None:
    # 5.0 must render as "5", not "5.0" -- matching JS `${5}` === "5".
    state = BuilderState(metric="up", comparison="==", threshold=5.0)
    assert generate_builder_expr(state) == "up == 5"


def test_generate_builder_expr_matches_brief_example() -> None:
    state = BuilderState(
        metric="metric",
        labels=[
            BuilderLabelFilter(key="k", op="=", value="v"),
            BuilderLabelFilter(key="k2", op="!~", value="v2"),
        ],
        comparison=">",
        threshold=5,
    )
    assert generate_builder_expr(state) == 'metric{k="v",k2!~"v2"} > 5'


def test_generate_builder_expr_quotes_embedded_double_quotes() -> None:
    state = BuilderState(
        metric="up",
        labels=[BuilderLabelFilter(key="job", op="=", value='has"quote')],
        comparison=">",
        threshold=0,
    )
    assert generate_builder_expr(state) == 'up{job="has\\"quote"} > 0'


@pytest.mark.parametrize(
    "threshold,expected",
    [
        (5, "5"),
        (0.5, "0.5"),
        (0.0001, "0.0001"),
        (0.00001, "1e-5"),
        (1e-7, "1e-7"),
        (123.456, "123.456"),
        (1e21, "1e+21"),
    ],
)
def test_generate_builder_expr_number_format_matches_frontend_pinned_vectors(
    threshold: float, expected: str
) -> None:
    # Pinned test vectors shared with the frontend's formatPromqlNumber
    # (builderExpr.ts) -- these specific values are exactly where Python's
    # repr() and JS's toString() natively disagree (both on when to switch
    # to scientific notation and on exponent zero-padding), which is why
    # _format_promql_number reimplements the fixed/scientific decision
    # itself rather than delegating to repr()/toString() directly.
    state = BuilderState(metric="metric", comparison=">", threshold=threshold)
    assert generate_builder_expr(state) == f"metric > {expected}"


def test_build_parse_round_trip_stays_builder_mode_with_tiny_threshold() -> None:
    # A threshold small enough to force scientific notation (1e-6) must
    # still round-trip through the annotation and be recognized as
    # mode=builder -- regression guard for the repr()/toString() format
    # divergence above.
    team = _team()
    state = BuilderState(metric="node_load1", comparison=">", threshold=1e-6)
    rule_input = RuleWrite(
        slug="tiny-threshold",
        alert_name="TinyThreshold",
        expr=generate_builder_expr(state),
        severity="warning",
        mode="builder",
        builder_state=state,
    )

    manifest = build_prometheus_rule(team, rule_input)
    parsed = parse_prometheus_rule(manifest)

    assert parsed["expr"] == "node_load1 > 1e-6"
    assert parsed["mode"] == "builder"
    assert parsed["builder_state"] == state.model_dump()


def test_rule_write_requires_builder_state_in_builder_mode() -> None:
    with pytest.raises(ValidationError):
        RuleWrite(
            slug="x",
            alert_name="X",
            expr="up > 0",
            severity="info",
            mode="builder",
        )


# -- threshold builder: annotation round trip -------------------------------


def test_build_stores_builder_state_as_annotation_in_builder_mode() -> None:
    team = _team()
    state = BuilderState(metric="node_load1", comparison=">", threshold=0)
    rule_input = RuleWrite(
        slug="high-load",
        alert_name="HighLoad",
        expr=generate_builder_expr(state),
        severity="warning",
        mode="builder",
        builder_state=state,
    )

    manifest = build_prometheus_rule(team, rule_input)

    # Deliberately k8s object metadata, not a rule-level annotation: the
    # Prometheus Operator's admission webhook runs rulefmt validation on
    # rule-level annotation *names* and rejects a dotted/slashed key like
    # this one outright ("invalid annotation name").
    meta_annotations = manifest["metadata"]["annotations"]
    assert BUILDER_STATE_ANNOTATION in meta_annotations
    stored = BuilderState.model_validate_json(meta_annotations[BUILDER_STATE_ANNOTATION])
    assert stored == state

    rule = manifest["spec"]["groups"][0]["rules"][0]
    assert BUILDER_STATE_ANNOTATION not in rule.get("annotations", {})


def test_build_omits_builder_annotation_in_promql_mode() -> None:
    team = _team()
    rule_input = RuleWrite(
        slug="hand-written",
        alert_name="HandWritten",
        expr="up == 0",
        severity="info",
        mode="promql",
    )

    manifest = build_prometheus_rule(team, rule_input)

    assert BUILDER_STATE_ANNOTATION not in manifest["metadata"].get("annotations", {})


def test_parse_reports_builder_mode_when_annotation_matches_stored_expr() -> None:
    team = _team()
    state = BuilderState(
        metric="node_load1",
        labels=[BuilderLabelFilter(key="job", op="=", value="node-exporter")],
        comparison=">",
        threshold=0,
    )
    rule_input = RuleWrite(
        slug="high-load",
        alert_name="HighLoad",
        expr=generate_builder_expr(state),
        for_="1m",
        severity="warning",
        mode="builder",
        builder_state=state,
    )

    manifest = build_prometheus_rule(team, rule_input)
    parsed = parse_prometheus_rule(manifest)

    assert parsed["mode"] == "builder"
    assert parsed["builder_state"] == state.model_dump()
    # The internal annotation is never surfaced as a user-facing annotation.
    assert BUILDER_STATE_ANNOTATION not in parsed["annotations"]


def test_parse_falls_back_to_promql_mode_when_expr_diverges_from_annotation() -> None:
    # Simulates: saved via the builder, then the user switched to PromQL
    # mode and hand-edited the expression without the annotation being
    # cleared out from under them (the stale-annotation-drop only happens
    # on the *next* promql-mode save, per build_prometheus_rule).
    team = _team()
    state = BuilderState(metric="node_load1", comparison=">", threshold=0)
    rule_input = RuleWrite(
        slug="high-load",
        alert_name="HighLoad",
        expr=generate_builder_expr(state),
        severity="warning",
        mode="builder",
        builder_state=state,
    )
    manifest = build_prometheus_rule(team, rule_input)
    # Hand-edit the stored expr directly, as if a promql-mode PUT had
    # changed it but (hypothetically) left the annotation behind.
    manifest["spec"]["groups"][0]["rules"][0]["expr"] = "node_load1 > 999"

    parsed = parse_prometheus_rule(manifest)

    assert parsed["mode"] == "promql"
    assert parsed["builder_state"] is None


def test_parse_ignores_malformed_builder_annotation() -> None:
    obj = {
        "metadata": {
            "name": "kam-t1-x",
            "labels": {"app.kubernetes.io/managed-by": "kam", "kam/team-id": "1"},
            "annotations": {BUILDER_STATE_ANNOTATION: "not-json"},
        },
        "spec": {
            "groups": [
                {
                    "name": "g",
                    "rules": [
                        {
                            "alert": "X",
                            "expr": "up > 0",
                            "labels": {"kam_team": "platform", "severity": "info"},
                        }
                    ],
                }
            ]
        },
    }

    parsed = parse_prometheus_rule(obj)
    assert parsed["mode"] == "promql"
    assert parsed["builder_state"] is None
    assert BUILDER_STATE_ANNOTATION not in parsed["annotations"]
