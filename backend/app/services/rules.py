"""PrometheusRule manifest builder/parser -- raw PromQL mode, plus the
Phase 5 threshold builder's round trip through a CRD annotation.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

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
BUILDER_STATE_ANNOTATION = "kam.io/builder-v1"

Severity = Literal["critical", "warning", "info"]
LabelOp = Literal["=", "!=", "=~", "!~"]
ComparisonOp = Literal[">", ">=", "<", "<=", "==", "!="]
RuleMode = Literal["builder", "promql"]


class BuilderLabelFilter(BaseModel):
    key: str = Field(min_length=1)
    op: LabelOp
    value: str


class BuilderState(BaseModel):
    """The threshold builder's guided-authoring state: a metric selector
    plus label filters, a comparison, and a threshold -- everything needed
    to regenerate the exact PromQL expression it produced.

    Deliberately excludes the rule's `for` duration: that's a CRD rule
    field independent of the expression itself (see `RuleWrite.for_`), not
    part of what `generate_builder_expr` renders.
    """

    metric: str = Field(min_length=1)
    labels: list[BuilderLabelFilter] = Field(default_factory=list)
    comparison: ComparisonOp
    threshold: float


def _format_promql_number(value: float) -> str:
    """Render `value` the way the frontend's JS `${value}` template
    interpolation would (e.g. 5.0 -> "5", 1.5 -> "1.5") -- this must match
    ThresholdBuilder.tsx's generator exactly, since it's compared byte-for-
    byte against the frontend-generated expr stored by `build_prometheus_rule`."""
    if value.is_integer():
        return str(int(value))
    return repr(value)


def _quote_promql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def generate_builder_expr(state: BuilderState) -> str:
    """Canonical PromQL generator for the threshold builder.

    Canonical format (must match the frontend's generator exactly -- this
    is what lets `parse_prometheus_rule` tell "still in sync with its
    builder_state" apart from "hand-edited after the fact"): no spaces
    inside the label-matcher braces, and exactly one space on each side of
    the comparison operator. Example: `metric{k="v",k2!~"v2"} > 5`.
    """
    if state.labels:
        matchers = ",".join(
            f'{lf.key}{lf.op}"{_quote_promql_string(lf.value)}"' for lf in state.labels
        )
        selector = f"{state.metric}{{{matchers}}}"
    else:
        selector = state.metric
    return f"{selector} {state.comparison} {_format_promql_number(state.threshold)}"


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
    mode: RuleMode = "promql"
    builder_state: BuilderState | None = None

    @model_validator(mode="after")
    def _builder_state_required_for_builder_mode(self) -> "RuleWrite":
        if self.mode == "builder" and self.builder_state is None:
            raise ValueError("builder_state is required when mode is 'builder'")
        return self


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
    if rule_input.mode == "builder" and rule_input.builder_state is not None:
        annotations[BUILDER_STATE_ANNOTATION] = rule_input.builder_state.model_dump_json()
    # A promql-mode save carries no builder_state, so the annotation is
    # simply never added here -- since replace_rule always submits a fresh
    # manifest (not a patch), any annotation from a prior builder-mode save
    # is naturally dropped rather than needing explicit removal.

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
    builder_state_raw = annotations.pop(BUILDER_STATE_ANNOTATION, None)
    expr = rule.get("expr", "")

    mode: RuleMode = "promql"
    builder_state: dict[str, Any] | None = None
    if builder_state_raw:
        try:
            parsed_state = BuilderState.model_validate_json(builder_state_raw)
        except ValidationError:
            parsed_state = None
        # Only trust the annotation as "builder mode" if regenerating PromQL
        # from it reproduces the expr actually stored on the rule -- a
        # hand-edit of the expr in PromQL mode (without clearing the stale
        # annotation) must not be reported back as builder mode.
        if parsed_state is not None and generate_builder_expr(parsed_state) == expr:
            mode = "builder"
            builder_state = parsed_state.model_dump()

    return {
        "slug": slug,
        "alert_name": rule.get("alert", ""),
        "expr": expr,
        "for": rule.get("for"),
        "severity": severity,
        "labels": labels,
        "annotations": annotations,
        "runbook_url": runbook_url,
        "grafana_url": grafana_url,
        "mode": mode,
        "builder_state": builder_state,
    }
