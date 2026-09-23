"""Cross-team alert sharing (Phase 14): pure evaluation of an `AlertShare`'s
matcher scope, plus the one query that resolves "who has shared alerts with
me" for the live/history read extension in `app/api/alerts.py`.

Matcher compilation/evaluation is reused from the routing engine
(`app.services.routing.compile_matchers`/`matcher_matches`) rather than
reimplemented -- a share's optional matcher list behaves identically to a
routing rule's: include is AND/`re.search`, exclude is OR/`re.search`, and
None/[] means "everything". Unlike a routing rule (which fails *open* on an
uncompilable pattern -- that one condition just never fires, degrading the
rule rather than breaking it), a share's matchers are what LIMITS how much
of another team's data crosses a team boundary: if any of them fails to
survive compilation, `share_matches`/`scope_matches` fail *closed* (deny)
rather than silently widening the share to "everything" by dropping the
only restriction the owner configured.

The `view_notify` fan-out itself (evaluating a *target* team's own routing
rules against a shared-in event) lives in `app.services.routing.route_event`,
which imports `share_matches` from here -- kept out of this module to avoid
a stateful/DB-touching function next to these two pure/near-pure ones.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.share import AlertShare
from app.services.routing import (
    CompiledMatcher,
    Matchable,
    build_transient_matchers,
    compile_matchers,
    matcher_matches,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MatchableAlert:
    """A minimal (alertname, labels, annotations) view satisfying
    `app.services.routing.Matchable` -- adapts a live Alertmanager alert
    dict (see `app/api/alerts.py`'s `_flatten`) to the same shape
    `AlertEvent` already has natively, so `share_matches` (and the routing
    matcher primitives it reuses) can evaluate either an ingested event or a
    live AM alert without knowing which one it's looking at.
    """

    alertname: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]

    @classmethod
    def from_live_alert(cls, alert: dict[str, Any]) -> "MatchableAlert":
        return cls(
            alertname=alert.get("alertname") or "",
            labels=alert.get("labels") or {},
            annotations=alert.get("annotations") or {},
        )


@dataclass(frozen=True)
class CompiledShareScope:
    """One `AlertShare`'s matcher scope, pre-compiled -- build this ONCE per
    share (via `compile_share_scope`) and reuse it across many
    `scope_matches` calls when checking a whole batch of alerts/events
    against the same share (`/alerts/live`'s per-alert loop, `/alerts/
    history`'s per-page post-filter), instead of paying `share_matches`'
    per-call recompilation cost for every single alert.

    `always_denies` is the fail-closed flag: if any of the share's stored
    matchers didn't survive `build_transient_matchers`/`compile_matchers`
    (a malformed row, or one whose pattern no longer compiles),
    `scope_matches` returns False unconditionally rather than evaluating
    with a silently-narrowed matcher set -- see this module's docstring for
    why that's the safe direction to fail for a security boundary.
    """

    include_matchers: tuple[CompiledMatcher, ...]
    exclude_matchers: tuple[CompiledMatcher, ...]
    always_denies: bool


def compile_share_scope(share: AlertShare) -> CompiledShareScope:
    """Compile `share.matchers` once. None/[] compiles to an always-matches
    (empty) scope -- see `CompiledShareScope`/`scope_matches`.
    """
    if not share.matchers:
        return CompiledShareScope(include_matchers=(), exclude_matchers=(), always_denies=False)

    transient = build_transient_matchers(share.matchers)
    include_matchers, exclude_matchers = compile_matchers(
        transient, context=f"alert_share matcher (share_id={share.id})"
    )
    always_denies = len(include_matchers) + len(exclude_matchers) < len(share.matchers)
    if always_denies:
        logger.warning(
            "alert_share matcher(s) failed to compile (share_id=%s) -- "
            "denying (fail-closed) rather than over-sharing",
            share.id,
        )
    return CompiledShareScope(include_matchers, exclude_matchers, always_denies)


def scope_matches(scope: CompiledShareScope, alert: Matchable) -> bool:
    """True if `alert` falls within a pre-compiled share scope. See
    `share_matches` for the one-shot (compile-and-check) convenience
    wrapper around this.
    """
    if scope.always_denies:
        return False
    if any(not matcher_matches(alert, m) for m in scope.include_matchers):
        return False
    return not any(matcher_matches(alert, m) for m in scope.exclude_matchers)


def share_matches(share: AlertShare, alert: Matchable) -> bool:
    """True if `alert` falls within `share`'s matcher scope -- a one-shot
    convenience wrapper (`compile_share_scope` + `scope_matches`) for
    checking a single alert/event against a single share: `route_event`'s
    per-event, per-share fan-out gate, `_authorize_event_read_access`'s
    single-event check, and tests. A caller checking MANY alerts against
    the SAME share (`/alerts/live`, `/alerts/history`) should call
    `compile_share_scope` once up front and reuse `scope_matches` directly
    instead, to avoid recompiling the same patterns on every alert.

    `alert` is anything satisfying `Matchable` -- an `AlertEvent` (history,
    `route_event`'s view_notify fan-out) or a `MatchableAlert` wrapping a
    live AM alert (`/alerts/live`'s read extension). `share.matchers` being
    `None`/`[]` means "every alert this team owns" -- no filtering at all.
    """
    return scope_matches(compile_share_scope(share), alert)


async def shared_source_team_ids(
    session: AsyncSession, viewer_team_id: int
) -> list[tuple[int, AlertShare]]:
    """Every `AlertShare` that targets `viewer_team_id`, as
    `(owner_team_id, share)` pairs -- one query, no caching (per-team share
    counts are expected to stay small). Used by `app/api/alerts.py`'s
    live/history read extension: the owner team id to widen the `kam_team`/
    `team_id` scope by, plus the share itself (mode, matchers) to compile a
    scope from via `compile_share_scope`.
    """
    result = await session.execute(
        select(AlertShare).where(AlertShare.target_team_id == viewer_team_id)
    )
    return [(share.owner_team_id, share) for share in result.scalars().all()]
