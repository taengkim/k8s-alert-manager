import os

# Must be set before any `app.*` import: app.config.get_settings() is
# @lru_cache'd and app.db reads it at module import time. The embedded
# outbox worker (app.worker.outbox.run_loop) would otherwise start polling
# the same in-memory sqlite db every test uses, racing test assertions
# about outbox row state.
os.environ.setdefault("KAM_WORKER_MODE", "off")

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
from app.db import Base, get_session
from app.main import create_app
from app.services.ldap_auth import LdapUserInfo


# Phase 21: KAM_DATABASE_URL, if set to a postgresql+... URL, points the
# whole test suite at a real Postgres instance instead of the default
# per-test in-memory SQLite db -- see docker-compose.dev.yml's `postgres`
# profile and deploy/README.md. This is the only way the Postgres branch of
# app.worker.outbox.claim_batch (FOR UPDATE SKIP LOCKED) gets exercised by
# an automated test (test_outbox_worker.py's concurrent-claim test skips
# itself when this isn't set). Unset (the default `uv run pytest`), nothing
# here changes: same in-memory SQLite URL as before.
_TEST_DATABASE_URL = os.environ.get("KAM_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
_IS_POSTGRES = _TEST_DATABASE_URL.startswith("postgresql")


@pytest.fixture
async def app() -> AsyncGenerator[FastAPI, None]:
    engine = create_async_engine(_TEST_DATABASE_URL)

    if not _IS_POSTGRES:
        # SQLite ignores FK constraints unless a connection explicitly turns
        # them on -- without this, tests wouldn't catch FK-violation bugs
        # that a real (Postgres) deployment would hit. Postgres enforces FKs
        # natively; there's no equivalent knob to set.
        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with session_factory() as session:
            yield session

    fastapi_app = create_app()
    fastapi_app.dependency_overrides[get_session] = override_get_session

    # The lifespan bootstrap (ensure_default_cluster) isn't part of the
    # request-scoped DI graph, so point the app's own session factory at the
    # same test db.
    db_module.async_session_factory = session_factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app

    if _IS_POSTGRES:
        # Unlike a fresh in-memory SQLite db (which simply vanishes when its
        # engine is disposed below), Postgres is one persistent database
        # shared across every test function -- each test gets a clean
        # schema by dropping everything this test created before the next
        # test's create_all runs.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest.fixture
async def client(app: FastAPI) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def login_as(
    client: AsyncClient,
    *,
    username: str,
    display_name: str | None = None,
    email: str | None = None,
    group_dns: list[str] | None = None,
    password: str = "password",
):
    """Monkeypatch ldap_auth.authenticate with a canned LdapUserInfo and log
    the given client in via POST /login. The client's cookie jar retains the
    session cookie for subsequent requests.
    """
    info = LdapUserInfo(
        dn=f"uid={username},ou=users,dc=example,dc=org",
        username=username,
        display_name=display_name or username.title(),
        email=email if email is not None else f"{username}@example.org",
        group_dns=group_dns or [],
    )

    def fake_authenticate(candidate_username: str, candidate_password: str):
        if candidate_username == username and candidate_password == password:
            return info
        return None

    with patch("app.api.auth.authenticate", fake_authenticate):
        response = await client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
    return response
