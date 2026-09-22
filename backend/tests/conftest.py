from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.db as db_module
from app.db import Base, get_session
from app.main import create_app
from app.services.ldap_auth import LdapUserInfo


@pytest.fixture
async def app() -> AsyncGenerator[FastAPI, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_session():
        async with session_factory() as session:
            yield session

    fastapi_app = create_app()
    fastapi_app.dependency_overrides[get_session] = override_get_session

    # The lifespan bootstrap (ensure_default_cluster) isn't part of the
    # request-scoped DI graph, so point the app's own session factory at the
    # same in-memory test db.
    db_module.async_session_factory = session_factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with fastapi_app.router.lifespan_context(fastapi_app):
        yield fastapi_app

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
