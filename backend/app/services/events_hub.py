"""In-process broadcast hub for the live alert SSE feed (Phase 18).

`Hub` is a plain in-memory pub/sub: `subscribe()` registers one
`asyncio.Queue` per connected SSE client (see `app/api/events.py`), and
`publish()` fans an event dict out to every subscriber whose team scope
covers it. Nothing here touches the database or does I/O -- it's pure
in-process state, safe to call from any request handler that already has a
`Hub` (via `request.app.state.events_hub`, one instance per app/process).

Publish timing (important -- see `publish_after_commit` below): every call
site in this codebase publishes AFTER its transaction has committed
successfully, never from inside one. `route_event`/`on_event_transition`
run pre-commit and only ever stage DB rows; they must never import or call
into this module. If a request fails after staging changes but before
`session.commit()`, nothing must have been published for it -- a
subscriber's queue has no rollback.

Multi-replica limitation (deliberately out of scope this phase): this hub's
subscriber queues live in ONE process's memory. With N>1 uvicorn workers or
Kubernetes replicas, a client connected to replica A never sees an event
published by replica B -- its live feed silently only reflects whichever
replica it happens to be attached to (TanStack Query's periodic refetch
still eventually corrects the view). The natural fix for a real
multi-replica deployment is Postgres LISTEN/NOTIFY: each replica would run
one background listener task subscribed to a channel this app NOTIFYs on
inside the same transaction an alert/ack/comment change commits in, and
every replica's listener re-publishes into its own local `Hub` on receipt.
Not implemented here -- this module is the natural swap-in point for that
listener once the app needs to run with more than one replica.
"""

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.services.ingest import AlertTransition, TransitionKind

logger = logging.getLogger(__name__)

# Every event this hub ever carries has one of these `type` values -- see
# each publish call site (app/api/webhook.py, app/api/alerts.py) for which
# transition produces which type.
EventType = Literal[
    "alert_created",
    "alert_resolved",
    "alert_reopened",
    "alert_acked",
    "alert_unacked",
    "comment_added",
]

# Bounded per-subscriber: a slow/stuck consumer (a tab left open, a flaky
# network) must never be able to grow its queue without limit and eat
# server memory. Once full, the OLDEST queued event is dropped to make room
# for the new one (see `Hub.publish`) -- a slow consumer misses history
# rather than blocking every other subscriber's publish.
QUEUE_MAXSIZE = 256

# What a subscriber's queue actually carries: the hub's own monotonic
# sequence number (used as the SSE `id:` field so a reconnecting
# EventSource's Last-Event-ID header is well-formed, even though this hub
# doesn't act on it -- see app/api/events.py's docstring) paired with the
# event dict itself.
QueueItem = tuple[int, dict[str, Any]]


@dataclass
class _Subscriber:
    queue: "asyncio.Queue[QueueItem]"
    team_ids: set[int]
    is_admin: bool


@dataclass
class Hub:
    """In-process pub/sub, one instance per app (see `app.main`'s lifespan,
    which stores it on `app.state.events_hub`).

    Subscriber scope: `is_admin=True` receives every event regardless of
    `team_id` (including `team_id is None`, i.e. an event unattributed to
    any team). A non-admin subscriber only receives events whose `team_id`
    is a member of the `team_ids` set it subscribed with -- an unattributed
    event (`team_id is None`) never reaches a non-admin subscriber.
    """

    _subscribers: dict[int, _Subscriber] = field(default_factory=dict)
    _next_subscriber_id: "itertools.count[int]" = field(default_factory=itertools.count)
    _next_seq: int = 0

    def subscribe(self, team_ids: set[int], is_admin: bool) -> tuple[int, "asyncio.Queue[QueueItem]"]:
        """Register a new subscriber and return `(subscriber_id, queue)`.
        The caller (the SSE endpoint) must call `unsubscribe(subscriber_id)`
        when the connection ends -- typically in a `finally` block, so a
        client that disconnects mid-stream doesn't leak a queue forever.
        """
        sub_id = next(self._next_subscriber_id)
        queue: asyncio.Queue[QueueItem] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers[sub_id] = _Subscriber(queue=queue, team_ids=set(team_ids), is_admin=is_admin)
        return sub_id, queue

    def unsubscribe(self, subscriber_id: int) -> None:
        self._subscribers.pop(subscriber_id, None)

    def publish(self, event: dict[str, Any]) -> None:
        """Fan `event` out to every subscriber whose scope covers it.

        Assigns the next hub-wide monotonic sequence number ONCE per
        publish call (shared across every subscriber that receives this
        event, not per-subscriber) -- see `QueueItem`.

        Never awaits: every subscriber queue op here is the non-blocking
        `_nowait` variant, so one slow subscriber can't stall this call (or
        any other subscriber's delivery) while `publish` is fanning out.
        """
        team_id = event.get("team_id")
        self._next_seq += 1
        seq = self._next_seq
        for subscriber_id, subscriber in list(self._subscribers.items()):
            if not subscriber.is_admin and (team_id is None or team_id not in subscriber.team_ids):
                continue
            self._deliver(subscriber_id, subscriber, seq, event)

    def _deliver(self, subscriber_id: int, subscriber: "_Subscriber", seq: int, event: dict[str, Any]) -> None:
        try:
            subscriber.queue.put_nowait((seq, event))
            return
        except asyncio.QueueFull:
            pass

        # Full: drop the oldest queued item to make room, then retry once.
        # A concurrent consumer could in principle drain a slot between the
        # get_nowait() and put_nowait() below (this hub has no other
        # producer for the SAME queue, so in practice this always
        # succeeds) -- if it still doesn't, this event is dropped too
        # rather than looping or blocking.
        try:
            subscriber.queue.get_nowait()
            logger.warning(
                "events_hub: subscriber %s queue full (maxsize=%s) -- dropping oldest event",
                subscriber_id,
                QUEUE_MAXSIZE,
            )
        except asyncio.QueueEmpty:
            pass
        try:
            subscriber.queue.put_nowait((seq, event))
        except asyncio.QueueFull:
            logger.warning(
                "events_hub: subscriber %s queue still full after drop -- dropping new event too",
                subscriber_id,
            )


def build_event(
    event_type: EventType,
    *,
    event_id: int,
    team_id: int | None,
    cluster: str,
    namespace: str | None = None,
    alertname: str,
    severity: str | None,
    is_test: bool,
) -> dict[str, Any]:
    """Build one hub event dict in the shape every subscriber/frontend
    consumer expects. `namespace` is an extra field beyond this phase's
    design doc's minimum event shape (type/event_id/team_id/cluster/
    alertname/severity/is_test/ts) -- included because the frontend's
    browser-notification body wants to show it alongside `cluster`, and
    `AlertEvent` already carries it at zero extra cost.
    """
    return {
        "type": event_type,
        "event_id": event_id,
        "team_id": team_id,
        "cluster": cluster,
        "namespace": namespace,
        "alertname": alertname,
        "severity": severity,
        "is_test": is_test,
        "ts": datetime.now(UTC).isoformat(),
    }


# Maps an `app.services.ingest.AlertTransition.kind` to the SSE event `type`
# it publishes as. Lives here (not in app.services.ingest, which has no
# other reason to know about this hub at all) since it's purely about how a
# transition becomes a hub event -- shared by every call site that walks an
# `IngestResult.transitions` list (app/api/webhook.py's webhook receiver,
# app/api/alerts.py's `fire_test_alert`).
TRANSITION_EVENT_TYPE: dict["TransitionKind", EventType] = {
    "created": "alert_created",
    "resolved": "alert_resolved",
    "reopened": "alert_reopened",
}


def build_event_from_transition(transition: "AlertTransition") -> dict[str, Any]:
    """`build_event`, specialized for one `app.services.ingest.AlertTransition`
    -- reads every field straight off `transition.event` (safe post-commit;
    see `AlertTransition`'s own docstring for why this doesn't need a
    pre-commit snapshot).
    """
    event = transition.event
    return build_event(
        TRANSITION_EVENT_TYPE[transition.kind],
        event_id=event.id,
        team_id=event.team_id,
        cluster=event.cluster_name,
        namespace=event.namespace,
        alertname=event.alertname,
        severity=event.severity,
        is_test=event.is_test,
    )


def publish_after_commit(hub: Hub, event: dict[str, Any]) -> None:
    """Publish `event` to `hub`. A thin, deliberately-named wrapper around
    `Hub.publish` -- exists so every call site reads as a documented
    contract rather than a bare `hub.publish(...)`:

        await session.commit()
        publish_after_commit(hub, build_event(...))

    MUST be called only after the enclosing transaction has committed
    successfully. Never call this (or `Hub.publish` directly) before/
    during a transaction: a later failure in the same request can still
    roll back everything staged so far, but an event already delivered to
    a subscriber's queue can't be un-delivered. This is exactly why
    `on_event_transition`/`route_event` (pre-commit) never import this
    module -- every publish call lives in the API layer, after its
    endpoint's own `await session.commit()`.
    """
    hub.publish(event)
