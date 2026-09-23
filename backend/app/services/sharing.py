"""Cross-team alert sharing (Phase 14): pure evaluation of an `AlertShare`'s
matcher scope, plus the one query that resolves "who has shared alerts with
me" for the live/history read extension in `app/api/alerts.py`.

`share_matches` reuses the routing engine's matcher compilation/evaluation
(`app.services.routing.compile_matchers`/`matcher_matches`) rather than
reimplementing include/exclude semantics -- a share's optional matcher list
behaves identically to a routing rule's: include is AND/`re.search`,
exclude is OR/`re.search`, and None/[] means "everything". The
`view_notify` fan-out itself (evaluating a *target* team's own routing
rules against a shared-in event) lives in `app.services.routing.route_event`,
which imports `share_matches` from here -- kept out of this module to avoid
a stateful/DB-touching function next to these two pure/near-pure ones.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.share import AlertShare
from app.services.routing import (
    Matchable,
    build_transient_matchers,
    compile_matchers,
    matcher_matches,
)


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


def share_matches(share: AlertShare, alert: Matchable) -> bool:
    """True if `alert` falls within `share`'s matcher scope.

    `alert` is anything satisfying `Matchable` -- an `AlertEvent` (history,
    `route_event`'s view_notify fan-out) or a `MatchableAlert` wrapping a
    live AM alert (`/alerts/live`'s read extension). `share.matchers` being
    `None`/`[]` means "every alert this team owns" -- no filtering at all.
    """
    if not share.matchers:
        return True

    transient = build_transient_matchers(share.matchers)
    include_matchers, exclude_matchers = compile_matchers(
        transient, context=f"alert_share matcher (share_id={share.id})"
    )
    if any(not matcher_matches(alert, m) for m in include_matchers):
        return False
    return not any(matcher_matches(alert, m) for m in exclude_matchers)


async def shared_source_team_ids(
    session: AsyncSession, viewer_team_id: int
) -> list[tuple[int, AlertShare]]:
    """Every `AlertShare` that targets `viewer_team_id`, as
    `(owner_team_id, share)` pairs -- one query, no caching (per-team share
    counts are expected to stay small). Used by `app/api/alerts.py`'s
    live/history read extension: the owner team id to widen the `kam_team`/
    `team_id` scope by, plus the share itself (mode, matchers) to filter
    with via `share_matches`.
    """
    result = await session.execute(
        select(AlertShare).where(AlertShare.target_team_id == viewer_team_id)
    )
    return [(share.owner_team_id, share) for share in result.scalars().all()]
