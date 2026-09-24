"""Heartbeat / deadman-switch sweep: the periodic half of Phase 17's
"monitoring the monitoring". `app.services.ingest`'s heartbeat hook (Phase 7,
extended in Phase 17 for recovery) handles the "a heartbeat arrived" side;
this module handles "a heartbeat stopped arriving".

State machine (`Cluster.heartbeat_state`):

    unknown --(first heartbeat)--> ok --(timeout elapses)--> missing
                                    ^                            |
                                    +---(a heartbeat arrives)----+

- 'unknown' (no heartbeat has ever been received for this cluster) NEVER
  alarms -- there is no established cadence yet to compare a missing beat
  against, so a cluster freshly registered (or one whose heartbeat has never
  once fired) simply sits here forever until its first heartbeat lands.
- The transition into 'missing' is edge-triggered and fires a synthetic
  alert exactly once, on the ok->missing edge: `sweep`'s own query below
  only ever inspects clusters currently 'ok', so once a cluster is 'missing'
  it is silently skipped on every subsequent tick -- no repeat alarm -- until
  `app.services.ingest`'s recovery extension flips it back to 'ok'.
- Recovery (missing -> ok) happens entirely on the ingest side, not here:
  see `app.services.ingest._resolve_heartbeat_lost_event`.

Concurrency: `heartbeat_state`/`last_heartbeat_at` are plain columns with no
lease or row lock -- this sweep (runs every 60s, see
`app/worker/outbox.py`'s `run_loop`) and a concurrent heartbeat delivery
hitting `app.services.ingest._ingest_one`'s hook simply race last-writer-wins
on the same row. At 60s sweep granularity against timeouts measured in
minutes (the brief's own dev default is 600s), the window where both could
touch the same row in the same instant is negligible, and the worst case is
one extra tick's delay before the "losing" write's effect is visible, not a
permanently wrong state -- the same trade-off `app/worker/scheduler.py`'s
module docstring documents for its own claim/dispatch bookkeeping.

Deliberately FastAPI-free, same reasoning as `app/worker/outbox.py` and
`app/worker/scheduler.py`: this runs embedded in the API process's lifespan
via `app/worker/outbox.py`'s `run_loop`, ticking alongside its own outbox
polling and `app/worker/scheduler.py`'s dispatch/retention ticks, all on the
one background task that loop already owns.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.cluster import Cluster
from app.models.team import Team
from app.services.ingest import (
    HEARTBEAT_LOST_ALERTNAME,
    AlertmanagerAlert,
    AlertmanagerWebhookPayload,
    heartbeat_lost_fingerprint,
    ingest_webhook,
)

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 60.0


class SweepSummary(TypedDict):
    clusters_checked: int
    went_missing: list[str]


async def _resolve_team_slug(session: AsyncSession, team_id: int | None) -> str | None:
    if team_id is None:
        return None
    result = await session.execute(select(Team.slug).where(Team.id == team_id))
    return result.scalar_one_or_none()


async def _inject_heartbeat_lost(session: AsyncSession, cluster: Cluster, now: datetime) -> None:
    """Fire one synthetic firing alert for `cluster` through the real ingest
    pipeline (`ingest_webhook`, the same entry point Alertmanager's own
    webhook delivery uses) -- suppress rules, templates, and storm control
    all apply exactly as they would to a real alert, per this phase's brief.

    `kam_team` is set from the cluster's `heartbeat_team_id` (resolved to
    that team's slug) only when one is configured -- omitting the label
    entirely when it isn't leaves `ingest_webhook`'s normal team resolution
    to land on `team_id=None`, which `route_event` already treats as
    "unassigned, routing skipped" (Phase 9 semantics): the missing cluster is
    still visible via `heartbeat_state`/the Alerts banner and alert history,
    just never routed to a channel nobody configured.
    """
    team_slug = await _resolve_team_slug(session, cluster.heartbeat_team_id)
    labels = {
        "alertname": HEARTBEAT_LOST_ALERTNAME,
        "cluster": cluster.name,
        "severity": "critical",
    }
    if team_slug is not None:
        labels["kam_team"] = team_slug

    alert = AlertmanagerAlert(
        status="firing",
        labels=labels,
        annotations={
            "description": (
                f"클러스터 '{cluster.display_name}'의 Watchdog 수신이 "
                f"{cluster.heartbeat_timeout_seconds}s 동안 끊겼습니다"
            )
        },
        startsAt=now.isoformat(),
        fingerprint=heartbeat_lost_fingerprint(cluster.id),
    )
    await ingest_webhook(session, cluster, AlertmanagerWebhookPayload(alerts=[alert]))


async def sweep(session_factory: async_sessionmaker[AsyncSession]) -> SweepSummary:
    """One heartbeat sweep tick.

    Only clusters currently `enabled`, `heartbeat_enabled`, and
    `heartbeat_state == 'ok'` are examined -- a disabled cluster, one with
    heartbeats turned off, one that has never sent a heartbeat ('unknown'),
    and one already 'missing' are all excluded by this query itself rather
    than by per-row branching below, which is what gives the state machine
    its "unknown never alarms" and "missing never re-alarms" guarantees for
    free: those clusters simply never reach the timeout check.

    `clusters_checked` counts exactly this query's candidates (regardless of
    whether any individual one turned out to have timed out) -- a plain
    per-tick "how many clusters did this sweep actually evaluate" figure for
    logging/monitoring, one row, one commit, mirroring
    `app.services.retention.purge`'s own per-target summary shape.
    """
    now = datetime.now(UTC)
    went_missing: list[str] = []

    async with session_factory() as session:
        result = await session.execute(
            select(Cluster).where(
                Cluster.enabled.is_(True),
                Cluster.heartbeat_enabled.is_(True),
                Cluster.heartbeat_state == "ok",
            )
        )
        candidates = list(result.scalars().all())

        for cluster in candidates:
            if cluster.last_heartbeat_at is None:
                # Defensive only: state=='ok' is always set together with
                # last_heartbeat_at by the ingest hook, so this should be
                # unreachable in practice -- guards one bad row rather than
                # crashing the whole sweep on it (a `None` here would raise
                # subtracting it from `now` below).
                logger.warning(
                    "heartbeat sweep: cluster=%s state='ok' but last_heartbeat_at is "
                    "unset -- skipping",
                    cluster.name,
                )
                continue

            elapsed = now - cluster.last_heartbeat_at
            if elapsed <= timedelta(seconds=cluster.heartbeat_timeout_seconds):
                continue

            if cluster.heartbeat_alertname == HEARTBEAT_LOST_ALERTNAME:
                # Would otherwise feed the very heartbeat hook this alert
                # exists to report the absence of (see
                # app.services.ingest._ingest_one's alertname match) --
                # extremely unlikely given the default ('Watchdog'), but
                # heartbeat_alertname is admin-configurable, so this is
                # checked rather than assumed.
                logger.warning(
                    "heartbeat sweep: cluster=%s heartbeat_alertname is set to the "
                    "synthetic alertname %r -- skipping injection to avoid feeding "
                    "its own heartbeat hook",
                    cluster.name,
                    HEARTBEAT_LOST_ALERTNAME,
                )
                continue

            cluster.heartbeat_state = "missing"
            await _inject_heartbeat_lost(session, cluster, now)
            went_missing.append(cluster.name)

        await session.commit()

    return {"clusters_checked": len(candidates), "went_missing": went_missing}
