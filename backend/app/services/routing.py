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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.channels.base import AlertNotification
from app.config import get_settings
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingMatcher, RoutingRule
from app.models.scheduled import ScheduledAction
from app.models.share import AlertShare
from app.models.team import Team
from app.services.grafana import resolve_grafana_url
from app.services.rules import RUNBOOK_ANNOTATION
from app.services.scheduled_actions import cancel_pending

logger = logging.getLogger(__name__)


@runtime_checkable
class Matchable(Protocol):
    """The (alertname, labels, annotations) shape a matcher reads from --
    satisfied structurally by `AlertEvent` as-is, and by Phase 14's
    `app.services.sharing.MatchableAlert` adapter for a live Alertmanager
    alert dict (`app/api/alerts.py`'s `_flatten`). This is what lets
    `matcher_matches`/`share_matches` evaluate either an ingested event or a
    live alert with identical matcher semantics.
    """

    alertname: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]


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


def compile_matchers(
    matchers: Sequence[RoutingMatcher], *, context: str
) -> tuple[tuple["CompiledMatcher", ...], tuple["CompiledMatcher", ...]]:
    """Compile a matcher list into (include, exclude) `CompiledMatcher`
    tuples, dropping (with a warning) any pattern that fails to compile --
    the shared primitive behind both `compile_rule` (a routing rule's own
    matchers) and `app.services.sharing.share_matches` (an `AlertShare`'s
    scope matchers, via `build_transient_matchers`), so a rule matcher and a
    share matcher use identical include/exclude compilation.

    Defensive by design: a pattern that fails to compile is logged and
    dropped rather than raising, since malformed data reaching this far
    (past API-layer validation) must degrade that one condition to "never
    fires," not take the whole routing/sharing pass down. `context` is
    folded into the warning so the log line still says which rule/share
    (and matcher position) misbehaved.
    """
    include_matchers: list[CompiledMatcher] = []
    exclude_matchers: list[CompiledMatcher] = []
    for matcher in sorted(matchers, key=lambda m: m.position):
        try:
            regex = re.compile(matcher.pattern)
        except re.error:
            logger.warning(
                "%s (position=%s) has invalid pattern %r -- skipping",
                context,
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

    return tuple(include_matchers), tuple(exclude_matchers)


def compile_rule(rule: RoutingRule, matchers: Sequence[RoutingMatcher]) -> CompiledRule:
    """Pre-compile a rule's regex-bearing fields.

    Defensive by design: a pattern that fails to compile is logged and
    dropped rather than raising, since malformed data reaching this far
    (past API-layer validation) must degrade the rule's matching to "this
    one condition never fires," not take the whole routing pass down.
    """
    severities = frozenset(s.lower() for s in rule.severities) if rule.severities else None

    include_matchers, exclude_matchers = compile_matchers(
        matchers, context=f"routing_rule matcher (rule_id={rule.id})"
    )

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
        include_matchers=include_matchers,
        exclude_matchers=exclude_matchers,
    )


def _matcher_value(matchable: Matchable, matcher: CompiledMatcher) -> str | None:
    if matcher.target == "alertname":
        return matchable.alertname
    if matcher.key is None:
        # label/annotation matcher with no key configured never matches --
        # API-layer validation requires a key for these targets, so this is
        # only reachable via a row that predates that validation.
        return None
    if matcher.target == "label":
        return matchable.labels.get(matcher.key)
    if matcher.target == "annotation":
        return matchable.annotations.get(matcher.key)
    return None


def matcher_matches(matchable: Matchable, matcher: CompiledMatcher) -> bool:
    """True if `matcher`'s pattern `re.search`-matches `matchable`'s value
    for its target (alertname/label/annotation) -- the single-matcher
    primitive both `evaluate_compiled`'s include/exclude loop and
    `app.services.sharing.share_matches` build on, so a routing rule
    matcher and an `AlertShare` matcher behave identically.
    """
    value = _matcher_value(matchable, matcher)
    return value is not None and bool(matcher.regex.search(value))


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
        if not matcher_matches(event, matcher):
            return Verdict(VerdictKind.NOT_INCLUDED, blocking_matcher_position=matcher.position)

    # 6. exclude matchers: OR, re.search.
    for matcher in compiled.exclude_matchers:
        if matcher_matches(event, matcher):
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
    # Both fields below are scoped to the OWNING team's own routing pass --
    # `routed`/`channels_notified` say nothing about whether any
    # `view_notify` share's target team was also notified (a target's own
    # routing, including its own suppress rules, is independent of the
    # owner's outcome -- see route_event's docstring). `reason="suppressed"`
    # in particular must never be read as "nothing was delivered anywhere":
    # `shared_channels_notified` surfaces the cross-team count so a caller
    # inspecting this outcome can't mistake owner-side suppression for a
    # total staging no-op.
    channels_notified: int = 0
    shared_channels_notified: int = 0


def _build_notification(
    event: AlertEvent, trigger: str, team_slug: str, cluster: Cluster | None
) -> AlertNotification:
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
        grafana_url=resolve_grafana_url(event.annotations, cluster, event.alertname),
    )


async def build_notification_for_event(
    session: AsyncSession, event: AlertEvent, trigger: str | None = None
) -> AlertNotification:
    """Public entry point for `_build_notification` for callers outside this
    module that need an `AlertNotification` for an *already-stored* event --
    the template preview API (`app/api/templates.py`), specifically, which
    has no routing outcome of its own to build one from. Resolves `team`
    (for `team_slug`) and `cluster` (for the Grafana deep link) itself, the
    same way `route_event` does.

    `trigger` defaults to the event's own stored `status` -- "what would
    this alert's actual notification have looked like" -- rather than
    always assuming "firing" the way route preview's evaluation does; a
    template author previewing against a resolved event presumably wants to
    see its resolved-shaped notification (`ends_at` populated, etc).
    """
    team = await session.get(Team, event.team_id) if event.team_id is not None else None
    cluster = await session.get(Cluster, event.cluster_id)
    return _build_notification(
        event, trigger if trigger is not None else event.status, team.slug if team else "", cluster
    )


async def _load_team_rules(
    session: AsyncSession, team_id: int, *, require_include_shared: bool
) -> tuple[list[RoutingRule], list[RoutingRule]]:
    """Load one team's enabled routing rules, split into (suppress, notify).

    `require_include_shared=True` narrows to rules with `include_shared=True`
    -- this is the Phase 14 gate: it's how a team's routing rules opt in to
    reacting to alerts *shared into* it (see `route_event`'s view_notify
    fan-out below). A team's own routing pass over its own events never sets
    this -- `include_shared` only matters for someone else's event reaching
    this team via an `AlertShare`, not for the team's own alert stream.
    """
    conditions = [RoutingRule.team_id == team_id, RoutingRule.enabled.is_(True)]
    if require_include_shared:
        conditions.append(RoutingRule.include_shared.is_(True))

    result = await session.execute(
        select(RoutingRule)
        .where(*conditions)
        .options(selectinload(RoutingRule.matchers), selectinload(RoutingRule.channels))
    )
    rules = result.scalars().all()
    return (
        [r for r in rules if r.action == "suppress"],
        [r for r in rules if r.action == "notify"],
    )


def _matched_notify_channels(
    event: AlertEvent, notify_rules: Sequence[RoutingRule], trigger: str
) -> dict[int, tuple[Channel, RoutingRule]]:
    """Every channel matched by any of `notify_rules`, deduped by channel id
    (a channel reachable via two matching rules gets exactly one outbox row
    per trigger, via the outbox UQ). A soft-deleted channel is skipped here
    -- staging a notification for it would just be delivered-to-dead-letter
    work for the worker to do instead of never creating it at all. Notify
    rules are NOT first-match-wins: every matching rule contributes its
    channels to this one union.
    """
    matched_channels: dict[int, tuple[Channel, RoutingRule]] = {}
    for rule in notify_rules:
        if evaluate(event, rule, rule.matchers, trigger=trigger).matched:
            for channel in rule.channels:
                if channel.deleted_at is not None:
                    continue
                matched_channels.setdefault(channel.id, (channel, rule))
    return matched_channels


async def _stage_outbox(
    session: AsyncSession,
    event: AlertEvent,
    trigger: str,
    team_id: int,
    matched_channels: dict[int, tuple[Channel, RoutingRule]],
    notification_payload: dict[str, Any],
) -> int:
    """Insert one outbox row per (channel, rule) in `matched_channels`,
    attributed to `team_id` -- the owning team for its own routing pass, or
    a share's target team for the Phase 14 view_notify fan-out. Shared by
    both call sites so the dedup-via-UQ handling (a repeated webhook
    delivery re-running the same transition) isn't duplicated.
    """
    created = 0
    for channel, rule in matched_channels.values():
        outbox = NotificationOutbox(
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            channel_id=channel.id,
            team_id=team_id,
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
            # delivery re-running the same transition, or -- for the shared
            # fan-out -- a channel reachable both directly and via a share).
            # Not an error.
            logger.info(
                "outbox dedup skip: event=%s channel=%s trigger=%s team=%s",
                event.id,
                channel.id,
                trigger,
                team_id,
            )
            continue
        created += 1
    return created


async def _schedule_escalations(
    session: AsyncSession, event: AlertEvent, notify_rules: Sequence[RoutingRule], trigger: str
) -> None:
    """Stage a `ScheduledAction(kind='escalation')` for each of `notify_rules`
    that both matched `event` and has escalation enabled (Phase 15) --
    dispatched later by `app/worker/scheduler.py`, which cancels it if the
    event is acknowledged (or resolved) before `due_at`.

    Scoped to the OWNING team's own notify rules only: this is called from
    `route_event`'s own routing pass, never from `_route_shared_view`'s
    target-team fan-out -- a share's target team escalating on behalf of
    another team's alert is out of this phase's scope.

    Gated on `trigger == "firing"`: escalation means "still unresolved after
    N minutes", which is meaningless for a resolved transition even if a
    rule with `notify_on_resolved` happens to also have escalation
    configured. At most one row per (event, rule) at a time -- a rule that
    already has a 'pending' escalation `ScheduledAction` for this event is
    skipped, so a repeated webhook delivery re-evaluating the same firing
    transition doesn't stack up duplicate timers.
    """
    if trigger != "firing":
        return
    for rule in notify_rules:
        if not rule.escalation_enabled or not rule.escalation_after_minutes:
            continue
        if not evaluate(event, rule, rule.matchers, trigger=trigger).matched:
            continue
        existing = await session.execute(
            select(ScheduledAction.id).where(
                ScheduledAction.alert_event_id == event.id,
                ScheduledAction.routing_rule_id == rule.id,
                ScheduledAction.kind == "escalation",
                ScheduledAction.status == "pending",
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue
        session.add(
            ScheduledAction(
                kind="escalation",
                alert_event_id=event.id,
                routing_rule_id=rule.id,
                due_at=datetime.now(UTC) + timedelta(minutes=rule.escalation_after_minutes),
                status="pending",
            )
        )


async def _route_shared_view(
    session: AsyncSession,
    event: AlertEvent,
    trigger: str,
    share: AlertShare,
    notification_payload: dict[str, Any],
) -> int:
    """Evaluate one `view_notify` share's target team against `event`,
    scoped to that team's own `include_shared=true` rules -- entirely
    independent of the owning team's own routing outcome (see
    `route_event`): a target's own suppress rule blocks only that target's
    notifications (no outbox rows staged for it), and never touches
    `event.suppressed_by_rule_id` -- that field records the *owning* team's
    suppression history, not a target's.

    Returns the number of outbox rows staged for this share's target team
    (0 if its own suppress rule blocked it, or no notify rule matched) --
    `route_event` sums this across every matching share into
    `RoutingOutcome.shared_channels_notified`.
    """
    suppress_rules, notify_rules = await _load_team_rules(
        session, share.target_team_id, require_include_shared=True
    )
    if any(evaluate(event, rule, rule.matchers, trigger=trigger).matched for rule in suppress_rules):
        return 0

    matched_channels = _matched_notify_channels(event, notify_rules, trigger)
    if not matched_channels:
        return 0
    return await _stage_outbox(
        session, event, trigger, share.target_team_id, matched_channels, notification_payload
    )


async def route_event(session: AsyncSession, event: AlertEvent, trigger: str) -> RoutingOutcome:
    """Stage outbox rows (or record a suppression) for one event transition,
    for the event's own (owning) team -- then, independently, fan the event
    out to every team its owner has shared it with in `view_notify` mode
    (Phase 14): each such target team's own `include_shared=true` rules get
    evaluated against the event too, gated by that share's optional matcher
    scope (`app.services.sharing.share_matches`). The owning team's outcome
    (including a suppress match) never blocks this fan-out -- suppression
    is a per-team decision, and a target's own routing (including its own
    suppress rules) is what decides whether *it* gets notified.

    Called inside the ingest transaction -- see this module's docstring and
    `app.services.ingest.on_event_transition`'s contract. Never commits;
    the caller's transaction covers this along with the event mutation that
    triggered it.
    """
    if trigger == "resolved":
        # Phase 15: there's nothing left to escalate or renotify about once
        # an event resolves -- cancel any 'pending' ScheduledAction for it,
        # regardless of whether this team has any escalation/renotify rules
        # configured at all. Covers both a real Alertmanager resolved
        # webhook (via app.services.ingest.on_event_transition) and
        # POST .../resolve-test (which calls route_event directly) --
        # route_event is the one place both paths already go through.
        await cancel_pending(session, event.id)

    if event.team_id is None:
        # No admin catch-all in this phase (post-MVP, see brief) -- an
        # event with no `kam_team` match just isn't routed (and can't be
        # shared -- a share's owner_team_id is always a real team).
        return RoutingOutcome(routed=False, reason="unassigned_team")

    suppress_rules, notify_rules = await _load_team_rules(
        session, event.team_id, require_include_shared=False
    )

    # Suppress rules are evaluated first, and exclusively: any match means
    # no notification at all for the owning team, regardless of what any
    # notify rule would have matched.
    suppressing_rule: RoutingRule | None = None
    for rule in suppress_rules:
        if evaluate(event, rule, rule.matchers, trigger=trigger).matched:
            suppressing_rule = rule
            break
    if suppressing_rule is not None:
        event.suppressed_by_rule_id = suppressing_rule.id

    matched_channels = (
        {} if suppressing_rule is not None else _matched_notify_channels(event, notify_rules, trigger)
    )
    if suppressing_rule is None:
        await _schedule_escalations(session, event, notify_rules, trigger)

    # Deferred, local import: app.services.sharing imports matcher
    # primitives from this module at import time, so importing it back at
    # module scope here would create a cycle. Importing it lazily, only
    # where it's actually used, breaks that without restructuring either
    # module.
    from app.services.sharing import share_matches

    shares_result = await session.execute(
        select(AlertShare).where(
            AlertShare.owner_team_id == event.team_id, AlertShare.mode == "view_notify"
        )
    )
    # Per-team share counts are expected to stay small (see the Phase 14
    # brief), so a plain Python filter here -- rather than trying to push
    # share_matches's matcher evaluation into the query -- is the right
    # trade-off.
    matching_shares = [s for s in shares_result.scalars().all() if share_matches(s, event)]

    if not matched_channels and not matching_shares:
        if suppressing_rule is not None:
            return RoutingOutcome(
                routed=False, reason="suppressed", suppressed_by_rule_id=suppressing_rule.id
            )
        return RoutingOutcome(routed=False, reason="no_match")

    team = await session.get(Team, event.team_id)
    assert team is not None  # event.team_id only ever points at a real team row

    # For the Grafana cluster-fallback link (see resolve_grafana_url) --
    # event.cluster_id always points at a real row via its FK, so this is
    # only None if the cluster was concurrently deleted between ingest and
    # routing, in which case the fallback simply degrades to "annotation
    # only, no cluster fallback" rather than failing the whole routing pass.
    cluster = await session.get(Cluster, event.cluster_id)

    # Built once -- identical for every channel this event routes to across
    # both the owning team and every shared target (only channel_id/team_id
    # differ per outbox row), so there's no reason to re-validate/
    # re-serialize an AlertNotification per channel.
    notification_payload = _build_notification(event, trigger, team.slug, cluster).model_dump(
        mode="json"
    )

    created = 0
    if matched_channels:
        created = await _stage_outbox(
            session, event, trigger, event.team_id, matched_channels, notification_payload
        )

    shared_channels_notified = 0
    for share in matching_shares:
        shared_channels_notified += await _route_shared_view(
            session, event, trigger, share, notification_payload
        )

    if suppressing_rule is not None:
        return RoutingOutcome(
            routed=False,
            reason="suppressed",
            suppressed_by_rule_id=suppressing_rule.id,
            shared_channels_notified=shared_channels_notified,
        )
    if not matched_channels:
        return RoutingOutcome(
            routed=False, reason="no_match", shared_channels_notified=shared_channels_notified
        )
    return RoutingOutcome(
        routed=created > 0,
        channels_notified=created,
        shared_channels_notified=shared_channels_notified,
    )


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
    """Adapt a plain matcher dict list -- a draft rule preview body
    (already validated by `app.api.routes`'s `MatcherInput`/
    `_validate_matchers` before it ever reaches here) or an `AlertShare`'s
    stored `matchers` JSON (not re-validated on every read) -- into
    transient (never session-added) `RoutingMatcher` rows for
    `compile_matchers`/`evaluate` to consume.

    A dict missing `kind`/`target`/`pattern` is skipped with a warning
    rather than raising `KeyError` -- the same fail-soft convention
    `compile_matchers` uses for a pattern that won't compile. A live
    create/update body can't produce one (API-layer validation already
    requires all three); this only guards a stored row that predates that
    validation, or a direct DB edit to `AlertShare.matchers`. Callers that
    treat "some matcher didn't survive" as security-relevant (e.g.
    `app.services.sharing.share_matches`, which must fail *closed* rather
    than silently widening a share's scope) compare their output count
    against the input `matchers` length themselves -- this function only
    logs and skips, it never signals "something was dropped" on its own.
    """
    result: list[RoutingMatcher] = []
    for position, m in enumerate(matchers):
        kind = m.get("kind")
        target = m.get("target")
        pattern = m.get("pattern")
        if kind is None or target is None or pattern is None:
            logger.warning(
                "matcher dict (position=%s) missing kind/target/pattern -- skipping: %r",
                position,
                m,
            )
            continue
        result.append(
            RoutingMatcher(kind=kind, target=target, key=m.get("key"), pattern=pattern, position=position)
        )
    return result
