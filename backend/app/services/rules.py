"""PrometheusRule manifest builder/parser -- raw PromQL mode.

A threshold builder (guided metric/comparison UI instead of hand-written
PromQL) is Phase 5; this phase only round-trips a fully user-authored
expression through a single-rule, single-group PrometheusRule CRD.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.models.team import Team

# No leading or trailing hyphen, max 63 chars total (1 + up to 61 + 1).
RULE_SLUG_RE = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
ALERT_NAME_RE = r"^[a-zA-Z_][a-zA-Z0-9_]*$"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
TEAM_ID_LABEL = "kam/team-id"
TEAM_SLUG_LABEL = "kam/team-slug"
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


def rule_object_name(team_id: int, slug: str) -> str:
    """The k8s object name for a team's rule.

    Keyed by the team's numeric id, not its slug: two different teams can
    have slugs that collide at a hyphen boundary (team "web" rule
    "api-latency" vs. team "web-api" rule "latency" would both stringify to
    "kam-web-api-latency" under slug-based naming). Ids are unique and
    unambiguous, so this can't happen.
    """
    return f"kam-t{team_id}-{slug}"


def build_prometheus_rule(team: Team, rule_input: RuleWrite) -> dict[str, Any]:
    """Build the full PrometheusRule CRD manifest for `rule_input`.

    Namespace is deliberately not set here: it's supplied separately by the
    k8s service (as `cluster.rules_namespace`) when the manifest is actually
    submitted, since this function has no cluster to consult.
    """
    name = rule_object_name(team.id, rule_input.slug)
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
                # Not used for ownership/identity (that's TEAM_ID_LABEL) --
                # purely so `kubectl get prometheusrule -l ...` reads as a
                # team name instead of a bare numeric id.
                TEAM_SLUG_LABEL: team.slug,
            },
        },
        "spec": {"groups": [{"name": group_name, "rules": [rule]}]},
    }


def parse_prometheus_rule(obj: dict[str, Any]) -> dict[str, Any]:
    """Inverse of `build_prometheus_rule`: extract the editable fields back
    out of a live PrometheusRule object.

    The object's own `kam/team-id` metadata label (always forced by
    `build_prometheus_rule`) is used to recover the `kam-t{id}-` name
    prefix to strip, rather than requiring a `team` argument here.
    """
    metadata = obj.get("metadata") or {}
    name = metadata.get("name", "")
    meta_labels = metadata.get("labels") or {}
    team_id = meta_labels.get(TEAM_ID_LABEL, "")

    groups = (obj.get("spec") or {}).get("groups") or []
    group = groups[0] if groups else {}
    rules = group.get("rules") or []
    rule = rules[0] if rules else {}

    labels = dict(rule.get("labels") or {})
    labels.pop(KAM_TEAM_LABEL, None)
    severity = labels.pop(SEVERITY_LABEL, "")

    prefix = f"kam-t{team_id}-"
    slug = name[len(prefix) :] if team_id and name.startswith(prefix) else name

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
