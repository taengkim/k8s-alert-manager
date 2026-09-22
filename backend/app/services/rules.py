"""PrometheusRule manifest builder/parser -- raw PromQL mode.

A threshold builder (guided metric/comparison UI instead of hand-written
PromQL) is Phase 5; this phase only round-trips a fully user-authored
expression through a single-rule, single-group PrometheusRule CRD.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.team import Team

RULE_SLUG_RE = r"^[a-z0-9][a-z0-9-]{0,62}$"
ALERT_NAME_RE = r"^[a-zA-Z_][a-zA-Z0-9_]*$"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
TEAM_ID_LABEL = "kam/team-id"
MANAGED_BY_VALUE = "kam"
KAM_TEAM_LABEL = "kam_team"
SEVERITY_LABEL = "severity"
RUNBOOK_ANNOTATION = "runbook_url"
GRAFANA_ANNOTATION = "kam.io/grafana-url"

Severity = Literal["critical", "warning", "info"]


class RuleWrite(BaseModel):
    """Request body for creating/updating a rule."""

    model_config = {"populate_by_name": True}

    slug: str = Field(pattern=RULE_SLUG_RE)
    alert_name: str = Field(pattern=ALERT_NAME_RE)
    expr: str = Field(min_length=1)
    for_: str | None = Field(default=None, alias="for")
    severity: Severity
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    runbook_url: str | None = None
    grafana_url: str | None = None


def build_prometheus_rule(team: Team, rule_input: RuleWrite) -> dict[str, Any]:
    """Build the full PrometheusRule CRD manifest for `rule_input`.

    Namespace is deliberately not set here: it's supplied separately by the
    k8s service (as `cluster.rules_namespace`) when the manifest is actually
    submitted, since this function has no cluster to consult.
    """
    name = f"kam-{team.slug}-{rule_input.slug}"
    group_name = f"kam-{team.slug}"

    labels = dict(rule_input.labels)
    labels[KAM_TEAM_LABEL] = team.slug
    labels[SEVERITY_LABEL] = rule_input.severity

    annotations = dict(rule_input.annotations)
    if rule_input.runbook_url:
        annotations[RUNBOOK_ANNOTATION] = rule_input.runbook_url
    if rule_input.grafana_url:
        annotations[GRAFANA_ANNOTATION] = rule_input.grafana_url

    rule: dict[str, Any] = {
        "alert": rule_input.alert_name,
        "expr": rule_input.expr,
        "labels": labels,
    }
    if rule_input.for_:
        rule["for"] = rule_input.for_
    if annotations:
        rule["annotations"] = annotations

    return {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {
            "name": name,
            "labels": {
                MANAGED_BY_LABEL: MANAGED_BY_VALUE,
                TEAM_ID_LABEL: str(team.id),
            },
        },
        "spec": {"groups": [{"name": group_name, "rules": [rule]}]},
    }


def parse_prometheus_rule(obj: dict[str, Any]) -> dict[str, Any]:
    """Inverse of `build_prometheus_rule`: extract the editable fields back
    out of a live PrometheusRule object.

    The rule's own `kam_team` label (always forced by `build_prometheus_rule`)
    is used to recover the team slug for stripping the `kam-{slug}-` name
    prefix, rather than requiring a `team` argument here.
    """
    metadata = obj.get("metadata") or {}
    name = metadata.get("name", "")

    groups = (obj.get("spec") or {}).get("groups") or []
    group = groups[0] if groups else {}
    rules = group.get("rules") or []
    rule = rules[0] if rules else {}

    labels = dict(rule.get("labels") or {})
    team_slug = labels.pop(KAM_TEAM_LABEL, "")
    severity = labels.pop(SEVERITY_LABEL, "")

    prefix = f"kam-{team_slug}-"
    slug = name[len(prefix) :] if team_slug and name.startswith(prefix) else name

    annotations = dict(rule.get("annotations") or {})
    runbook_url = annotations.pop(RUNBOOK_ANNOTATION, None)
    grafana_url = annotations.pop(GRAFANA_ANNOTATION, None)

    return {
        "slug": slug,
        "alert_name": rule.get("alert", ""),
        "expr": rule.get("expr", ""),
        "for": rule.get("for"),
        "severity": severity,
        "labels": labels,
        "annotations": annotations,
        "runbook_url": runbook_url,
        "grafana_url": grafana_url,
    }
