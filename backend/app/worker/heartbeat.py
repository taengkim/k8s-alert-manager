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
  see `app.services.ingest.resolve_heartbeat_lost_event` (also called
  directly from `app/api/clusters.py`'s `update_cluster` when an admin
  disables a cluster, or turns its `heartbeat_enabled` off, while it's
  'missing' -- see that module for why).

Per-cluster isolation: each candidate is evaluated and (if timed out)
flipped in its OWN session/transaction (`_check_and_flip_one`), with a
per-cluster try/except around it in `sweep`'s loop. A failure injecting one
cluster's synthetic alert (e.g. `route_event` raising on a misconfigured
routing rule, a DB hiccup, ...) is logged and skipped -- it can neither roll
back nor block any other candidate in the same tick, and that cluster
simply gets re-evaluated (and, if still timed out, retried) on the next
60s tick.

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


async def _check_and_flip_one(
    session_factory: async_sessionmaker[AsyncSession], cluster_id: int, now: datetime
) -> str | None:
    """Evaluate exactly one candidate cluster and, if it has timed out, flip
    it to 'missing' and inject its synthetic alert -- all in a single fresh
    session/transaction scoped to this one cluster, so a failure here (this
    function is NOT wrapped in its own try/except; `sweep`'s loop does that)
    can only ever affect this cluster's own commit, never another
    candidate's. Returns the cluster's name if it just went missing, else
    `None`.

    Re-checks `enabled`/`heartbeat_enabled`/`heartbeat_state` against this
    fresh read rather than trusting `sweep`'s id-only query result: a
    concurrent change (an admin disabling this cluster, a heartbeat
    arriving) between that query and this row's own load here could have
    already moved it out of the window it was selected for.
    """
    async with session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        if cluster is None:
            return None
        if not cluster.enabled or not cluster.heartbeat_enabled or cluster.heartbeat_state != "ok":
            return None

        if cluster.last_heartbeat_at is None:
            # Defensive only: state=='ok' is always set together with
            # last_heartbeat_at by the ingest hook, so this should be
            # unreachable in practice -- guards one bad row rather than
            # raising subtracting `None` from `now` below.
            logger.warning(
                "heartbeat sweep: cluster=%s state='ok' but last_heartbeat_at is "
                "unset -- skipping",
                cluster.name,
            )
            return None

        elapsed = now - cluster.last_heartbeat_at
        if elapsed <= timedelta(seconds=cluster.heartbeat_timeout_seconds):
            return None

        if cluster.heartbeat_alertname == HEARTBEAT_LOST_ALERTNAME:
            # Would otherwise feed the very heartbeat hook this alert exists
            # to report the absence of (see
            # app.services.ingest._ingest_one's alertname match) --
            # extremely unlikely given the default ('Watchdog'), but
            # heartbeat_alertname is admin-configurable, so this is checked
            # rather than assumed.
            logger.warning(
                "heartbeat sweep: cluster=%s heartbeat_alertname is set to the "
                "synthetic alertname %r -- skipping injection to avoid feeding "
                "its own heartbeat hook",
                cluster.name,
                HEARTBEAT_LOST_ALERTNAME,
            )
            return None

        cluster.heartbeat_state = "missing"
        await _inject_heartbeat_lost(session, cluster, now)
        await session.commit()
        return cluster.name


async def sweep(session_factory: async_sessionmaker[AsyncSession]) -> SweepSummary:
    """One heartbeat sweep tick.

    Only clusters currently `enabled`, `heartbeat_enabled`, and
    `heartbeat_state == 'ok'` are candidates -- a disabled cluster, one with
    heartbeats turned off, one that has never sent a heartbeat ('unknown'),
    and one already 'missing' are all excluded by this query itself rather
    than by per-row branching, which is what gives the state machine its
    "unknown never alarms" and "missing never re-alarms" guarantees for
    free: those clusters simply never reach the timeout check.

    `clusters_checked` counts exactly this query's candidates (regardless of
    whether any individual one turned out to have timed out, or failed) -- a
    plain per-tick "how many clusters did this sweep actually evaluate"
    figure for logging/monitoring, mirroring
    `app.services.retention.purge`'s own per-target summary shape.

    Each candidate is evaluated/flipped in its own session via
    `_check_and_flip_one` (see that function and this module's docstring for
    why) -- a per-cluster failure is logged here and skipped, never allowed
    to propagate and abort the rest of the tick.
    """
    now = datetime.now(UTC)
    went_missing: list[str] = []

    async with session_factory() as session:
        result = await session.execute(
            select(Cluster.id).where(
                Cluster.enabled.is_(True),
                Cluster.heartbeat_enabled.is_(True),
                Cluster.heartbeat_state == "ok",
            )
        )
        candidate_ids = [row[0] for row in result.all()]

    for cluster_id in candidate_ids:
        try:
            name = await _check_and_flip_one(session_factory, cluster_id, now)
        except Exception:
            logger.exception(
                "heartbeat sweep: failed to process cluster id=%s -- skipping this "
                "tick, will re-evaluate next tick",
                cluster_id,
            )
            continue
        if name is not None:
            went_missing.append(name)

    return {"clusters_checked": len(candidate_ids), "went_missing": went_missing}
