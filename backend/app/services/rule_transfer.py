"""Portable JSON export/import for a team's rules.

An export envelope carries only the fields `parse_prometheus_rule` already
strips ownership metadata from (see `app/services/rules.py`): no
managed-by/team-id labels, no `kam-t{id}-` name prefix. Re-targeting a rule
to a different team/cluster on import is therefore just "run the normal
`build_prometheus_rule(target_team, rule_input)` pipeline against the target
cluster" -- the same manifest builder every create/update already uses,
rather than anything import-specific.

`plan_import` (dry-run) and `execute_import` (real writes) share one
pipeline (`_run_import`, toggled by a `dry_run` flag) so a preview can never
drift from what actually happens on "적용" -- the whole point of a dry-run
being trustworthy.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from pydantic import ValidationError

from app.config import get_settings
from app.models.cluster import Cluster
from app.models.team import Team
from app.services.k8s import (
    K8sBadRequestError,
    K8sClientFactory,
    K8sUnavailableError,
    RuleConflictError,
    RuleForbiddenError,
    RuleUpdateConflictError,
)
from app.services.prometheus import PrometheusClient
from app.services.rules import RuleWrite, build_prometheus_rule, rule_object_name

KAM_EXPORT_VERSION = 1

ConflictStrategy = Literal["skip", "overwrite", "rename"]
VerdictAction = Literal["created", "skipped", "overwritten", "renamed", "failed"]

# How many renamed candidates (-2, -3, ...) to try before giving up -- purely
# a safety valve against a pathological/malicious batch, never expected to
# bite in practice.
_MAX_RENAME_ATTEMPTS = 1000

_EXPORTED_RULE_FIELDS = (
    "slug",
    "alert_name",
    "expr",
    "for",
    "severity",
    "labels",
    "annotations",
    "runbook_url",
    "grafana_url",
    "mode",
    "builder_state",
)


class UnsupportedExportVersion(Exception):
    """Raised when an import envelope isn't a `kind: "rules"`,
    `kam_export_version: 1` document this build knows how to read."""


class _NoAvailableRenameSlug(Exception):
    """Internal: `_MAX_RENAME_ATTEMPTS` candidates were all taken."""


@dataclass
class RuleVerdict:
    """The outcome of importing one rule -- identical shape whether it came
    from a dry-run (`plan_import`) or a real write (`execute_import`)."""

    slug: str
    action: VerdictAction
    final_slug: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "action": self.action,
            "final_slug": self.final_slug,
            "errors": self.errors,
            "warnings": self.warnings,
        }


@dataclass
class ParsedRuleEntry:
    """One rule from an import envelope, after schema validation.

    `rule` is None when this entry failed `RuleWrite` validation -- carried
    through as data (not raised) so one malformed rule in a batch surfaces as
    that rule's own `failed` verdict rather than rejecting the whole import.
    `slug` is a best-effort label for reporting: the envelope's own `slug`
    field when present (even if the rest of the rule doesn't validate),
    otherwise "?".
    """

    slug: str
    rule: RuleWrite | None
    error: str | None = None


@dataclass
class ParsedEnvelope:
    source: dict[str, Any]
    entries: list[ParsedRuleEntry]


def build_export_envelope(
    team: Team, cluster: Cluster, rules: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a portable export envelope from a list of already-parsed rule
    dicts (i.e. `parse_prometheus_rule(...)` output -- ownership metadata is
    already stripped there, so nothing here needs to re-scrub it).

    Only the whitelisted `_EXPORTED_RULE_FIELDS` are carried over: a caller
    that (like the rules-list endpoint) has merged in `health`/`state`/
    `last_error` onto the same dicts won't leak that live-status noise into
    the portable file.
    """
    settings = get_settings()
    return {
        "kam_export_version": KAM_EXPORT_VERSION,
        "kind": "rules",
        "exported_at": datetime.now(UTC).isoformat(),
        "source": {
            "team_slug": team.slug,
            "cluster_name": cluster.name,
            "app_version": settings.app_version,
        },
        "rules": [{field: rule.get(field) for field in _EXPORTED_RULE_FIELDS} for rule in rules],
    }


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}" for err in exc.errors()
    )


def parse_envelope(data: Any) -> ParsedEnvelope:
    """Validate an import envelope's shape and each of its rules.

    Only the envelope-level shape (version/kind/rules-is-a-list) is fatal
    (`UnsupportedExportVersion`, mapped to a 400 by the API layer) -- a
    single rule failing `RuleWrite` validation is captured as a `failed`
    verdict-to-be instead, so one bad row doesn't block the rest of the
    batch from even being previewed.
    """
    if not isinstance(data, dict):
        raise UnsupportedExportVersion("export data must be a JSON object")

    version = data.get("kam_export_version")
    if version != KAM_EXPORT_VERSION:
        raise UnsupportedExportVersion(
            f"unsupported kam_export_version: {version!r} "
            f"(this build only supports {KAM_EXPORT_VERSION})"
        )

    if data.get("kind") != "rules":
        raise UnsupportedExportVersion(
            f"unsupported export kind: {data.get('kind')!r} (expected 'rules')"
        )

    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list):
        raise UnsupportedExportVersion("export data is missing a 'rules' list")

    entries: list[ParsedRuleEntry] = []
    for raw in raw_rules:
        raw_slug = raw.get("slug") if isinstance(raw, dict) else None
        display_slug = raw_slug if isinstance(raw_slug, str) and raw_slug else "?"
        try:
            rule = RuleWrite.model_validate(raw)
        except ValidationError as exc:
            entries.append(
                ParsedRuleEntry(slug=display_slug, rule=None, error=_format_validation_error(exc))
            )
            continue
        entries.append(ParsedRuleEntry(slug=rule.slug, rule=rule))

    return ParsedEnvelope(source=data.get("source") or {}, entries=entries)


async def _find_rename_slug(
    k8s: K8sClientFactory,
    cluster: Cluster,
    team_id: int,
    base_slug: str,
    claimed: set[str],
) -> str:
    """First `{base_slug}-2`, `{base_slug}-3`, ... not already claimed
    earlier in this same batch and not already present on the target
    cluster."""
    for n in range(2, _MAX_RENAME_ATTEMPTS):
        suffix = f"-{n}"
        base = base_slug[: 63 - len(suffix)] if len(base_slug) + len(suffix) > 63 else base_slug
        candidate = f"{base}{suffix}"
        if candidate in claimed:
            continue
        existing = await k8s.get_rule(cluster, rule_object_name(team_id, candidate))
        if existing is None:
            return candidate
    raise _NoAvailableRenameSlug()


async def _run_import(
    k8s: K8sClientFactory,
    http_client: httpx.AsyncClient,
    *,
    team: Team,
    target_cluster: Cluster,
    entries: list[ParsedRuleEntry],
    conflict_strategy: ConflictStrategy,
    dry_run: bool,
) -> list[RuleVerdict]:
    prometheus = PrometheusClient(target_cluster, http_client)
    verdicts: list[RuleVerdict] = []
    # Slugs already claimed by an earlier rule *in this same batch* (skipped
    # conflicts included, so a later rule can't rename onto a slug a prior
    # skip left alone) -- on top of whatever `k8s.get_rule` reports as
    # already present on the cluster.
    claimed: set[str] = set()

    for entry in entries:
        if entry.rule is None:
            verdicts.append(
                RuleVerdict(slug=entry.slug, action="failed", errors=[entry.error or "invalid rule"])
            )
            continue

        rule_input = entry.rule
        slug = rule_input.slug

        # Prometheus being unreachable is a cluster-wide precondition
        # failure, not a per-rule one -- deliberately left to propagate as
        # PrometheusUnavailableError so the API layer aborts the whole batch
        # with a 503, the same treatment create/update rule already give it,
        # rather than reporting every single rule as individually invalid.
        validation = await prometheus.validate_query(rule_input.expr)
        if not validation["valid"]:
            verdicts.append(
                RuleVerdict(
                    slug=slug,
                    action="failed",
                    errors=[f"invalid PromQL expression: {validation['error']}"],
                )
            )
            continue

        name = rule_object_name(team.id, slug)
        try:
            existing = await k8s.get_rule(target_cluster, name)
        except (K8sBadRequestError, K8sUnavailableError) as exc:
            verdicts.append(RuleVerdict(slug=slug, action="failed", errors=[str(exc)]))
            continue

        conflict = existing is not None or slug in claimed

        final_slug = slug
        action: VerdictAction
        if not conflict:
            action = "created"
        elif conflict_strategy == "skip":
            verdicts.append(RuleVerdict(slug=slug, action="skipped"))
            claimed.add(slug)
            continue
        elif conflict_strategy == "overwrite":
            action = "overwritten"
        else:
            try:
                final_slug = await _find_rename_slug(k8s, target_cluster, team.id, slug, claimed)
            except _NoAvailableRenameSlug:
                verdicts.append(
                    RuleVerdict(slug=slug, action="failed", errors=["no available rename slug"])
                )
                continue
            action = "renamed"

        claimed.add(final_slug)
        write_rule_input = (
            rule_input if final_slug == slug else rule_input.model_copy(update={"slug": final_slug})
        )
        # Re-targeting happens for free here: build_prometheus_rule stamps
        # `team`'s own labels/name-prefix regardless of which team the
        # envelope was originally exported from, and writing to
        # `target_cluster` below is what re-targets the cluster.
        manifest = build_prometheus_rule(team, write_rule_input)

        if dry_run:
            verdicts.append(
                RuleVerdict(slug=slug, action=action, final_slug=final_slug if action == "renamed" else None)
            )
            continue

        try:
            if action == "overwritten":
                await k8s.replace_rule(target_cluster, name, team.id, manifest)
            else:
                await k8s.create_rule(target_cluster, manifest)
        except (
            RuleConflictError,
            RuleForbiddenError,
            RuleUpdateConflictError,
            K8sBadRequestError,
            K8sUnavailableError,
        ) as exc:
            verdicts.append(RuleVerdict(slug=slug, action="failed", errors=[str(exc)]))
            continue

        verdicts.append(
            RuleVerdict(slug=slug, action=action, final_slug=final_slug if action == "renamed" else None)
        )

    return verdicts


async def plan_import(
    k8s: K8sClientFactory,
    http_client: httpx.AsyncClient,
    *,
    team: Team,
    target_cluster: Cluster,
    entries: list[ParsedRuleEntry],
    conflict_strategy: ConflictStrategy,
) -> list[RuleVerdict]:
    """Dry-run: identical pipeline to `execute_import`, but never calls
    `k8s.create_rule`/`k8s.replace_rule` -- zero writes."""
    return await _run_import(
        k8s,
        http_client,
        team=team,
        target_cluster=target_cluster,
        entries=entries,
        conflict_strategy=conflict_strategy,
        dry_run=True,
    )


async def execute_import(
    k8s: K8sClientFactory,
    http_client: httpx.AsyncClient,
    *,
    team: Team,
    target_cluster: Cluster,
    entries: list[ParsedRuleEntry],
    conflict_strategy: ConflictStrategy,
) -> list[RuleVerdict]:
    """Real import: same pipeline as `plan_import`, with the create/replace
    calls actually made."""
    return await _run_import(
        k8s,
        http_client,
        team=team,
        target_cluster=target_cluster,
        entries=entries,
        conflict_strategy=conflict_strategy,
        dry_run=False,
    )


__all__ = [
    "KAM_EXPORT_VERSION",
    "ParsedEnvelope",
    "ParsedRuleEntry",
    "RuleVerdict",
    "UnsupportedExportVersion",
    "build_export_envelope",
    "execute_import",
    "parse_envelope",
    "plan_import",
]
