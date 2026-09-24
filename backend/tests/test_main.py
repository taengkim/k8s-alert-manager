"""Startup warnings for insecure default secrets (see app.main.lifespan).

Each test enters the lifespan context directly against its own isolated
in-memory engine (same pattern as
test_channel_registry.py::test_entry_point_channel_missing_type_name_is_skipped)
rather than using the shared `app`/`client` fixtures -- those fixtures
already resolve `app` before a test body runs, so there'd be no way to wrap
the lifespan entry (where the warnings are logged) in `caplog.at_level`.
"""

import logging
from unittest.mock import patch

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
from app.config import get_settings
from app.db import Base
from app.main import create_app


async def _enter_lifespan_with_fresh_db() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    db_module.async_session_factory = session_factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    fastapi_app = create_app()
    async with fastapi_app.router.lifespan_context(fastapi_app):
        pass

    await engine.dispose()


async def test_default_secret_key_and_webhook_token_warn_on_startup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _enter_lifespan_with_fresh_db()

    messages = [rec.message for rec in caplog.records]
    assert any("KAM_SECRET_KEY" in m for m in messages)
    assert any("KAM_WEBHOOK_TOKEN" in m for m in messages)


async def test_no_warning_when_secret_key_and_webhook_token_overridden(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = get_settings()
    with (
        patch.object(settings, "secret_key", "a-real-production-secret"),
        patch.object(settings, "webhook_token", "a-real-production-token"),
        caplog.at_level(logging.WARNING, logger="app.main"),
    ):
        await _enter_lifespan_with_fresh_db()

    messages = [rec.message for rec in caplog.records]
    assert not any("KAM_SECRET_KEY" in m for m in messages)
    assert not any("KAM_WEBHOOK_TOKEN" in m for m in messages)
