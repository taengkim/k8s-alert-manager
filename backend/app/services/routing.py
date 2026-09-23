"""The routing engine: decides which of a team's routing rules match a
given alert event, and stages outbox rows for the ones that do.

`compile_rule`/`evaluate` are pure functions with no I/O -- they're the unit
tested surface (see tests/test_routing_engine.py's evaluation-order
matrix). `route_event` is the only piece that touches the database, and it
is called from `app.services.ingest.on_event_transition` *inside* the
ingest transaction: per that hook's contract, this only stages
`NotificationOutbox` rows (and `AlertEvent.suppressed_by_rule_id`) -- it
never dispatches anything externally. Delivery is `app/worker/outbox.py`'s
job, running against rows this leaves in `status='pending'`.
"""

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.channels.base import AlertNotification
from app.config import get_settings
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingMatcher, RoutingRule
from app.models.team import Team
from app.services.rules import GRAFANA_ANNOTATION, RUNBOOK_ANNOTATION

logger = logging.getLogger(__name__)


class VerdictKind(str, Enum):
    MATCHED = "matched"
    CLUSTER_FILTERED = "cluster_filtered"
    GATED = "gated"
    SEVERITY_FILTERED = "severity_filtered"
    NAMESPACE_FILTERED = "namespace_filtered"
    NOT_INCLUDED = "not_included"
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    # Only set for NOT_INCLUDED/EXCLUDED -- the matcher (by its `position`,
    # not database id, so a not-yet-persisted preview draft can report one
    # too) that blocked the match.
    blocking_matcher_position: int | None = None

    @property
    def matched(self) -> bool:
        return self.kind is VerdictKind.MATCHED


@dataclass(frozen=True)
class CompiledMatcher:
    position: int
    kind: str  # 'include' | 'exclude'
    target: str  # 'alertname' | 'label' | 'annotation'
    key: str | None
    regex: re.Pattern[str]


@dataclass(frozen=True)
class CompiledRule:
    action: str
    enabled: bool
    notify_on_firing: bool
    notify_on_resolved: bool
    clusters: frozenset[int] | None
    severities: frozenset[str] | None
    namespaces_include: tuple[re.Pattern[str], ...]
    namespaces_exclude: tuple[re.Pattern[str], ...]
    include_matchers: tuple[CompiledMatcher, ...]
    exclude_matchers: tuple[CompiledMatcher, ...]


def _compile_patterns(patterns: Sequence[str] | None, *, context: str) -> list[re.Pattern[str]]:
    if not patterns:
        return []
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern))
        except re.error:
            # Server-side validation (app/api/routes.py) rejects an
            # uncompilable pattern at save time -- this is only a backstop
            # against a row that predates that validation (or a direct DB
            # edit). Skipping it disables just that one pattern, not the
            # whole rule.
            logger.warning("%s: invalid regex %r -- skipping", context, pattern)
    return compiled


def compile_rule(rule: RoutingRule, matchers: Sequence[RoutingMatcher]) -> CompiledRule:
    """Pre-compile a rule's regex-bearing fields.

    Defensive by design: a pattern that fails to compile is logged and
    dropped rather than raising, since malformed data reaching this far
    (past API-layer validation) must degrade the rule's matching to "this
    one condition never fires," not take the whole routing pass down.
    """
    severities = frozenset(s.lower() for s in rule.severities) if rule.severities else None

    include_matchers: list[CompiledMatcher] = []
    exclude_matchers: list[CompiledMatcher] = []
    for matcher in sorted(matchers, key=lambda m: m.position):
        try:
            regex = re.compile(matcher.pattern)
        except re.error:
            logger.warning(
                "routing_rule matcher (rule_id=%s, position=%s) has invalid pattern %r "
                "-- skipping",
                rule.id,
                matcher.position,
                matcher.pattern,
            )
            continue
        compiled = CompiledMatcher(
            position=matcher.position,
            kind=matcher.kind,
            target=matcher.target,
            key=matcher.key,
            regex=regex,
        )
        (include_matchers if matcher.kind == "include" else exclude_matchers).append(compiled)

    return CompiledRule(
        action=rule.action,
        enabled=rule.enabled,
        notify_on_firing=rule.notify_on_firing,
        notify_on_resolved=rule.notify_on_resolved,
        clusters=frozenset(rule.clusters) if rule.clusters else None,
        severities=severities,
        namespaces_include=tuple(
            _compile_patterns(rule.namespaces_include, context="namespaces_include")
        ),
        namespaces_exclude=tuple(
            _compile_patterns(rule.namespaces_exclude, context="namespaces_exclude")
        ),
        include_matchers=tuple(include_matchers),
        exclude_matchers=tuple(exclude_matchers),
    )


def _matcher_value(event: AlertEvent, matcher: CompiledMatcher) -> str | None:
    if matcher.target == "alertname":
        return event.alertname
    if matcher.key is None:
        # label/annotation matcher with no key configured never matches --
        # API-layer validation requires a key for these targets, so this is
        # only reachable via a row that predates that validation.
        return None
    if matcher.target == "label":
        return event.labels.get(matcher.key)
    if matcher.target == "annotation":
        return event.annotations.get(matcher.key)
    return None


def evaluate_compiled(
    event: AlertEvent, compiled: CompiledRule, *, trigger: str | None = None
) -> Verdict:
    """Evaluate one pre-compiled rule against one event, cheapest checks
    first.

    `trigger` drives the notify_on_firing/notify_on_resolved gate; when
    omitted it defaults to `event.status`, which is correct for
    `route_event` (by the time that runs, inside `on_event_transition`,
    the event's status has already been updated to reflect the transition
    being routed -- trigger and status are the same value there). Preview
    passes `trigger="firing"` explicitly instead: the question a preview
    answers is "would this rule have notified when this alert fired",
    regardless of whether the stored event has since resolved.

    A 'suppress' rule ignores the notify_on_firing/resolved gate entirely
    (it always evaluates, regardless of trigger) -- suppression is a "never
    notify for this" decision, not a firing-vs-resolved preference.
    """
    effective_trigger = trigger if trigger is not None else event.status

    # 0. clusters filter.
    if compiled.clusters is not None and event.cluster_id not in compiled.clusters:
        return Verdict(VerdictKind.CLUSTER_FILTERED)

    # 1. gate: enabled, then (notify rules only) trigger-specific notify_on.
    if not compiled.enabled:
        return Verdict(VerdictKind.GATED)
    if compiled.action != "suppress":
        if effective_trigger == "firing" and not compiled.notify_on_firing:
            return Verdict(VerdictKind.GATED)
        if effective_trigger == "resolved" and not compiled.notify_on_resolved:
            return Verdict(VerdictKind.GATED)

    # 2. severities.
    if compiled.severities is not None:
        if event.severity is None:
            if "none" not in compiled.severities:
                return Verdict(VerdictKind.SEVERITY_FILTERED)
        elif event.severity.lower() not in compiled.severities:
            return Verdict(VerdictKind.SEVERITY_FILTERED)

    # 3. namespaces_include: anchored (fullmatch). A NULL namespace only
    # passes when there's no include filter at all (handled by the guard
    # above already returning True in that case).
    if compiled.namespaces_include and (
        event.namespace is None
        or not any(p.fullmatch(event.namespace) for p in compiled.namespaces_include)
    ):
        return Verdict(VerdictKind.NAMESPACE_FILTERED)

    # 4. namespaces_exclude: anchored (fullmatch). A NULL namespace is
    # never excluded.
    if (
        compiled.namespaces_exclude
        and event.namespace is not None
        and any(p.fullmatch(event.namespace) for p in compiled.namespaces_exclude)
    ):
        return Verdict(VerdictKind.NAMESPACE_FILTERED)

    # 5. include matchers: AND, re.search.
    for matcher in compiled.include_matchers:
        value = _matcher_value(event, matcher)
        if value is None or not matcher.regex.search(value):
            return Verdict(VerdictKind.NOT_INCLUDED, blocking_matcher_position=matcher.position)

    # 6. exclude matchers: OR, re.search.
    for matcher in compiled.exclude_matchers:
        value = _matcher_value(event, matcher)
        if value is not None and matcher.regex.search(value):
            return Verdict(VerdictKind.EXCLUDED, blocking_matcher_position=matcher.position)

    return Verdict(VerdictKind.MATCHED)


def evaluate(
    event: AlertEvent,
    rule: RoutingRule,
    matchers: Sequence[RoutingMatcher],
    *,
    trigger: str | None = None,
) -> Verdict:
    """Compile `rule`+`matchers` and evaluate `event` against it -- the
    convenience entry point for one-off calls (tests, route_event's
    per-rule loop), where compiling once per call is cheap enough:
    Python's own `re.compile` keeps an internal cache keyed by pattern
    string, so re-compiling an identical pattern on every call is
    effectively free. Callers evaluating one rule against MANY events
    (`preview_rule`) should call `compile_rule` once up front and use
    `evaluate_compiled` directly instead of paying per-event overhead
    (and, for `compile_rule` itself, per-event log spam on any invalid
    pattern).
    """
    return evaluate_compiled(event, compile_rule(rule, matchers), trigger=trigger)


@dataclass
class RoutingOutcome:
    routed: bool
    reason: str | None = None  # 'unassigned_team' | 'suppressed' | 'no_match' | None
    suppressed_by_rule_id: int | None = None
    channels_notified: int = 0


def _build_notification(event: AlertEvent, trigger: str, team_slug: str) -> AlertNotification:
    settings = get_settings()
    return AlertNotification(
        event_id=event.id,
        trigger=trigger,  # type: ignore[arg-type]
        alertname=event.alertname,
        severity=event.severity,
        namespace=event.namespace,
        cluster=event.cluster_name,
        labels=event.labels,
        annotations=event.annotations,
        starts_at=event.starts_at,
        ends_at=event.ends_at,
        team_slug=team_slug,
        app_url=f"{settings.app_base_url}/alerts/history/{event.id}",
        runbook_url=event.annotations.get(RUNBOOK_ANNOTATION),
        grafana_url=event.annotations.get(GRAFANA_ANNOTATION),
    )


async def route_event(session: AsyncSession, event: AlertEvent, trigger: str) -> RoutingOutcome:
    """Stage outbox rows (or record a suppression) for one event transition.

    Called inside the ingest transaction -- see this module's docstring and
    `app.services.ingest.on_event_transition`'s contract. Never commits;
    the caller's transaction covers this along with the event mutation that
    triggered it.
    """
    if event.team_id is None:
        # No admin catch-all in this phase (post-MVP, see brief) -- an
        # event with no `kam_team` match just isn't routed.
        return RoutingOutcome(routed=False, reason="unassigned_team")

    result = await session.execute(
        select(RoutingRule)
        .where(RoutingRule.team_id == event.team_id, RoutingRule.enabled.is_(True))
        .options(selectinload(RoutingRule.matchers), selectinload(RoutingRule.channels))
    )
    rules = result.scalars().all()

    suppress_rules = [r for r in rules if r.action == "suppress"]
    notify_rules = [r for r in rules if r.action == "notify"]

    # Suppress rules are evaluated first, and exclusively: any match means
    # no notification at all, regardless of what any notify rule would have
    # matched.
    for rule in suppress_rules:
        if evaluate(event, rule, rule.matchers, trigger=trigger).matched:
            event.suppressed_by_rule_id = rule.id
            return RoutingOutcome(routed=False, reason="suppressed", suppressed_by_rule_id=rule.id)

    # Notify rules are NOT first-match-wins: every matching rule contributes
    # its channels to one union, deduped by channel id (a channel reachable
    # via two matching rules gets exactly one outbox row per trigger, via
    # the UQ below). A soft-deleted channel is skipped here -- staging a
    # notification for it would just be delivered-to-dead-letter work for
    # the worker to do instead of never creating it at all.
    matched_channels: dict[int, tuple[Channel, RoutingRule]] = {}
    for rule in notify_rules:
        if evaluate(event, rule, rule.matchers, trigger=trigger).matched:
            for channel in rule.channels:
                if channel.deleted_at is not None:
                    continue
                matched_channels.setdefault(channel.id, (channel, rule))

    if not matched_channels:
        return RoutingOutcome(routed=False, reason="no_match")

    team = await session.get(Team, event.team_id)
    assert team is not None  # event.team_id only ever points at a real team row

    # Built once -- identical for every channel this event routes to (only
    # channel_id differs per outbox row), so there's no reason to
    # re-validate/re-serialize an AlertNotification per channel.
    notification_payload = _build_notification(event, trigger, team.slug).model_dump(mode="json")

    created = 0
    for channel, rule in matched_channels.values():
        outbox = NotificationOutbox(
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            channel_id=channel.id,
            team_id=event.team_id,
            trigger=trigger,
            payload=dict(notification_payload),
        )
        try:
            async with session.begin_nested():
                session.add(outbox)
                await session.flush()
        except IntegrityError:
            # (alert_event_id, channel_id, trigger) UQ -- this transition
            # was already routed to this channel (e.g. a repeated webhook
            # delivery re-running the same transition). Not an error.
            logger.info(
                "outbox dedup skip: event=%s channel=%s trigger=%s",
                event.id,
                channel.id,
                trigger,
            )
            continue
        created += 1

    return RoutingOutcome(routed=created > 0, channels_notified=created)


@dataclass(frozen=True)
class PreviewResult:
    event_id: int
    alertname: str
    severity: str | None
    namespace: str | None
    cluster: str
    status: str
    verdict: VerdictKind
    blocking_matcher_position: int | None = None


async def preview_rule(
    session: AsyncSession,
    team_id: int,
    rule: RoutingRule,
    matchers: Sequence[RoutingMatcher],
    *,
    limit: int = 200,
) -> list[PreviewResult]:
    """Evaluate a draft rule (never persisted) against a team's most
    recent alert events, for the route editor's preview panel.

    Always evaluates as `trigger="firing"`, regardless of an event's
    current stored status -- the question this answers is "would this
    rule have notified when this alert fired", which for a since-resolved
    event is still "as if it had just fired", not "as if it resolved".
    `compile_rule` is hoisted out of the per-event loop: it's identical for
    every event this draft is evaluated against, so compiling (and
    logging any invalid pattern) once instead of up to `limit` times.
    """
    compiled = compile_rule(rule, matchers)

    result = await session.execute(
        select(AlertEvent)
        .where(AlertEvent.team_id == team_id)
        .order_by(AlertEvent.last_received_at.desc(), AlertEvent.id.desc())
        .limit(limit)
    )
    events = result.scalars().all()

    previews: list[PreviewResult] = []
    for event in events:
        verdict = evaluate_compiled(event, compiled, trigger="firing")
        previews.append(
            PreviewResult(
                event_id=event.id,
                alertname=event.alertname,
                severity=event.severity,
                namespace=event.namespace,
                cluster=event.cluster_name,
                status=event.status,
                verdict=verdict.kind,
                blocking_matcher_position=verdict.blocking_matcher_position,
            )
        )
    return previews


def build_transient_rule(*, team_id: int, **fields: Any) -> RoutingRule:
    """A `RoutingRule` instance that's never added to a session -- used by
    the preview API to evaluate a draft body without persisting it.
    """
    return RoutingRule(team_id=team_id, **fields)


def build_transient_matchers(matchers: Sequence[dict[str, Any]]) -> list[RoutingMatcher]:
    return [
        RoutingMatcher(
            kind=m["kind"],
            target=m["target"],
            key=m.get("key"),
            pattern=m["pattern"],
            position=position,
        )
        for position, m in enumerate(matchers)
    ]
