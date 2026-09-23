import re

import pytest

from app.models.team import Team
from app.services.rules import (
    RULE_SLUG_RE,
    RuleWrite,
    build_prometheus_rule,
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
    assert rule["annotations"]["kam.io/grafana-url"] == "https://grafana.example.com/d/disk"


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
