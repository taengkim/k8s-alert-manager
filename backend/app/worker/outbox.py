"""The outbox delivery worker: claims `NotificationOutbox` rows staged by
`app.services.routing.route_event` and dispatches them through the channel
they're addressed to.

Deliberately FastAPI-free (no `fastapi` import anywhere in this module, nor
transitively via its imports) -- this is meant to be runnable as a
standalone process (see `app/worker/runner.py`) as well as embedded in the
API process's lifespan (see `app/main.py`).
"""

import asyncio
import json
import logging
import random
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.channels.base import AlertNotification
from app.channels.registry import ChannelRegistry
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.security import decrypt_str

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 1800
JITTER_MAX_SECONDS = 15
DELIVERY_TIMEOUT_SECONDS = 30
DEFAULT_LEASE_TIMEOUT = timedelta(minutes=5)


async def claim_batch(
    session: AsyncSession, worker_id: str, limit: int = 20
) -> list[NotificationOutbox]:
    """Atomically claim up to `limit` due 'pending' rows for this worker,
    marking them 'in_progress'.

    Dialect-branched: Postgres uses `FOR UPDATE SKIP LOCKED` so multiple
    worker processes can claim disjoint batches concurrently without
    blocking on each other. SQLite has no such row-locking, so it falls
    back to plain SELECT-then-UPDATE under the (documented) assumption that
    only a single worker process runs against a SQLite database at a time.
    """
    now = datetime.now(UTC)
    dialect = session.get_bind().dialect.name

    if dialect == "postgresql":
        due_ids_subquery = (
            select(NotificationOutbox.id)
            .where(
                NotificationOutbox.status == "pending",
                NotificationOutbox.next_attempt_at <= now,
            )
            .order_by(NotificationOutbox.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        claimed = await session.execute(
            update(NotificationOutbox)
            .where(NotificationOutbox.id.in_(due_ids_subquery))
            .values(status="in_progress", locked_by=worker_id, locked_at=now)
            .returning(NotificationOutbox.id)
        )
        claimed_ids = list(claimed.scalars().all())
        await session.commit()
        if not claimed_ids:
            return []
        result = await session.execute(
            select(NotificationOutbox).where(NotificationOutbox.id.in_(claimed_ids))
        )
        return list(result.scalars().all())

    # SQLite: single-process assumption -- no concurrent worker can race
    # this claim, so a plain select-then-update is safe.
    result = await session.execute(
        select(NotificationOutbox)
        .where(
            NotificationOutbox.status == "pending",
            NotificationOutbox.next_attempt_at <= now,
        )
        .order_by(NotificationOutbox.next_attempt_at)
        .limit(limit)
    )
    rows = list(result.scalars().all())
    for row in rows:
        row.status = "in_progress"
        row.locked_by = worker_id
        row.locked_at = now
    await session.commit()
    return rows


def _truncate_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


async def _mark_dead(session: AsyncSession, row: NotificationOutbox, reason: str) -> None:
    row.status = "dead"
    row.last_error = reason[:500]
    row.locked_by = None
    row.locked_at = None
    await session.commit()


async def _mark_delivery_failure(
    session: AsyncSession, row: NotificationOutbox, exc: Exception
) -> None:
    row.attempts += 1
    row.last_error = _truncate_error(exc)
    row.locked_by = None
    row.locked_at = None
    if row.attempts >= MAX_ATTEMPTS:
        row.status = "dead"
    else:
        row.status = "pending"
        backoff = min(BASE_BACKOFF_SECONDS * 2**row.attempts, MAX_BACKOFF_SECONDS)
        jitter = random.uniform(0, JITTER_MAX_SECONDS)
        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff + jitter)
    await session.commit()


async def deliver(row: NotificationOutbox, registry: ChannelRegistry, session: AsyncSession) -> None:
    """Deliver one claimed row. Always resolves the row to a terminal-ish
    state and commits -- 'delivered', 'pending' (scheduled for retry), or
    'dead' -- never leaves it 'in_progress' on return.
    """
    channel = await session.get(Channel, row.channel_id)
    if channel is None or not channel.enabled:
        await _mark_dead(session, row, "channel disabled or deleted")
        return

    channel_cls = registry.get(channel.type)
    if channel_cls is None:
        await _mark_dead(session, row, f"unknown channel type '{channel.type}'")
        return

    try:
        raw_config = json.loads(decrypt_str(channel.config_encrypted))
        config = channel_cls.config_schema(**raw_config)
        instance = channel_cls(config)
        notification = AlertNotification(**row.payload)
        async with asyncio.timeout(DELIVERY_TIMEOUT_SECONDS):
            await instance.send(notification)
    except Exception as exc:  # noqa: BLE001
        # Deliberately catch-all (ChannelDeliveryError, the asyncio.timeout
        # block's TimeoutError, and anything else a channel's send() or a
        # bad stored config could raise): every failure mode gets the same
        # backoff-and-retry treatment, never an unhandled exception that
        # would kill the worker loop.
        await _mark_delivery_failure(session, row, exc)
        return

    row.status = "delivered"
    row.delivered_at = datetime.now(UTC)
    row.locked_by = None
    row.locked_at = None
    await session.commit()


async def recover_stale_leases(
    session: AsyncSession, lease_timeout: timedelta = DEFAULT_LEASE_TIMEOUT
) -> int:
    """Reclaim rows a worker claimed but never resolved (crashed, killed
    mid-delivery, ...) so they become claimable again instead of stuck
    'in_progress' forever.
    """
    cutoff = datetime.now(UTC) - lease_timeout
    result = await session.execute(
        update(NotificationOutbox)
        .where(NotificationOutbox.status == "in_progress", NotificationOutbox.locked_at < cutoff)
        .values(status="pending", locked_by=None, locked_at=None)
    )
    await session.commit()
    return result.rowcount or 0


async def run_tick(
    session_factory: async_sessionmaker[AsyncSession],
    registry: ChannelRegistry,
    worker_id: str,
    *,
    limit: int = 20,
) -> int:
    """Claim and deliver one batch. Returns how many rows were claimed."""
    async with session_factory() as session:
        rows = await claim_batch(session, worker_id, limit=limit)
        for row in rows:
            await deliver(row, registry, session)
        return len(rows)


async def run_loop(
    stop_event: asyncio.Event,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    registry: ChannelRegistry,
    worker_id: str,
    poll_interval: float = 3.0,
    lease_recovery_interval: float = 60.0,
    lease_timeout: timedelta = DEFAULT_LEASE_TIMEOUT,
) -> None:
    """Poll for due outbox rows until `stop_event` is set.

    Robust by design: any exception during a tick (claim, deliver, or lease
    recovery) is logged and swallowed so one bad iteration never kills the
    loop -- the next poll just tries again.
    """
    last_lease_recovery = time.monotonic() - lease_recovery_interval  # run once immediately
    while not stop_event.is_set():
        try:
            if time.monotonic() - last_lease_recovery >= lease_recovery_interval:
                async with session_factory() as session:
                    recovered = await recover_stale_leases(session, lease_timeout)
                    if recovered:
                        logger.warning("outbox worker: recovered %d stale lease(s)", recovered)
                last_lease_recovery = time.monotonic()

            await run_tick(session_factory, registry, worker_id)
        except Exception:
            logger.exception("outbox worker: tick failed -- continuing")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval)
        except TimeoutError:
            pass
