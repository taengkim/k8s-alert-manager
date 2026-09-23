"""Standalone outbox worker process: `python -m app.worker.runner`.

Builds its own engine/session factory/channel registry rather than sharing
`app.db`'s module-level engine or `app.main`'s app.state -- this is meant to
run as a separate OS process from the API server (`KAM_WORKER_MODE=off` on
the API side), so it can't depend on FastAPI's lifespan having set anything
up.
"""

import asyncio
import logging
import signal
import socket
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.channels.registry import ChannelRegistry
from app.config import get_settings
from app.db import register_sqlite_pragmas
from app.worker.outbox import run_loop

logger = logging.getLogger(__name__)


def _worker_id() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()

    engine = create_async_engine(settings.database_url)
    register_sqlite_pragmas(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    registry = ChannelRegistry()
    registry.discover()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # add_signal_handler isn't available on Windows's default
            # event loop -- the process still exits on Ctrl+C, it just
            # won't get a chance to finish an in-flight delivery first.
            pass

    worker_id = _worker_id()
    logger.info("outbox worker starting (worker_id=%s)", worker_id)
    try:
        await run_loop(
            stop_event,
            session_factory=session_factory,
            registry=registry,
            worker_id=worker_id,
        )
    finally:
        await engine.dispose()
        logger.info("outbox worker stopped (worker_id=%s)", worker_id)


if __name__ == "__main__":
    asyncio.run(main())
