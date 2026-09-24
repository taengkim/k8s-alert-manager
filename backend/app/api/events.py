"""Live alert feed: `GET /api/v1/events/stream` (SSE, Phase 18).

Design (see design doc §B7): Server-Sent Events, not WebSocket -- this feed
is one-directional (server -> client), and EventSource gives auto-reconnect
and cookie auth for free, with no extra proxy configuration. The endpoint
itself is a thin adapter: authenticate, resolve this user's subscription
scope, register with the in-process `Hub` (`app/services/events_hub.py`),
and stream whatever the hub delivers until the client disconnects.

Last-Event-ID replay is NOT supported. The hub keeps no event log -- it's
pure fan-out over live subscriber queues -- so a reconnecting EventSource's
`Last-Event-ID` header (which this endpoint doesn't even read) can't be
used to replay a gap. Whatever a client misses while disconnected is
recovered the same way any other staleness is: the frontend's own
TanStack Query invalidation (this same stream's messages, when connected)
and periodic refetch intervals already in place on the live/history views.
This is a deliberate scope boundary for this phase, not an oversight -- see
`app.services.events_hub`'s module docstring for the (also out of scope)
multi-replica story this would need solving alongside a real replay log.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user
from app.db import get_session
from app.models.team import TeamMembership
from app.models.user import User
from app.services.events_hub import Hub, QueueItem

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/events", tags=["events"])

# How often sse-starlette emits a `: ping` comment line while the queue is
# otherwise idle -- keeps intermediary proxies/load balancers from timing
# out an apparently-idle long-lived connection, and gives EventSource
# something to notice if the underlying TCP connection silently died.
PING_INTERVAL_SECONDS = 15


def get_hub(request: Request) -> Hub:
    return request.app.state.events_hub


async def _member_team_ids(session: AsyncSession, user: User) -> set[int]:
    result = await session.execute(
        select(TeamMembership.team_id).where(TeamMembership.user_id == user.id)
    )
    return {team_id for (team_id,) in result.all()}


async def _stream(
    hub: Hub, subscriber_id: int, queue: asyncio.Queue[QueueItem]
) -> AsyncIterator[dict[str, str]]:
    """Yield sse-starlette event dicts until the client disconnects.

    sse-starlette closes this async generator (raising `GeneratorExit`
    inside the `await queue.get()`) once it detects the client is gone --
    the `finally` here is what actually unregisters the subscriber, so a
    disconnected client's queue doesn't leak forever (see `Hub.unsubscribe`).
    """
    try:
        while True:
            seq, event = await queue.get()
            yield {
                "event": event["type"],
                "id": str(seq),
                "data": json.dumps(event, ensure_ascii=False),
            }
    finally:
        hub.unsubscribe(subscriber_id)


@router.get("/stream")
async def stream_events(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    hub: Hub = Depends(get_hub),
) -> EventSourceResponse:
    """Subscribe scope: an admin receives every event (including ones with
    no team, e.g. an alert whose `kam_team` label never matched a team) --
    a non-admin only receives events for teams they're a member of. This
    mirrors `/alerts/live`'s own admin-sees-everything / member-sees-own-
    teams split.

    The hub exists on `app.state` regardless of `KAM_WORKER_MODE` (unlike
    the outbox worker, which the test app fixture turns off) -- this
    endpoint has no notion of a "worker" at all, so there's nothing to gate.
    """
    team_ids = set() if user.is_admin else await _member_team_ids(session, user)
    subscriber_id, queue = hub.subscribe(team_ids, user.is_admin)

    return EventSourceResponse(_stream(hub, subscriber_id, queue), ping=PING_INTERVAL_SECONDS)
