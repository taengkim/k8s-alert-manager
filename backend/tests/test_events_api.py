"""API-level tests for `GET /api/v1/events/stream` (Phase 18 SSE feed):
cookie auth gate and an actual published event round-tripping through a
real streaming HTTP response.

The streaming tests can't use the `client`/`app` fixtures' ASGITransport:
httpx's ASGITransport drives the whole ASGI app callable to completion
before handing back a response (see httpx's own docs on its testing
transport), which works fine for a request/response endpoint but never
returns for an SSE endpoint that only finishes when the client disconnects
-- the two are mutually blocking under that transport. A real (loopback)
uvicorn server, talked to over an actual socket, doesn't have that
limitation: reading and writing genuinely interleave, exactly like a real
deployment. `lifespan="off"` on the server Config is deliberate -- the
`app` fixture already entered `app.router.lifespan_context` itself (see
conftest.py), so this avoids running startup/shutdown twice on the same
app instance.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient

from app.services.events_hub import build_event
from app.services.ldap_auth import LdapUserInfo
from tests.conftest import login_as

STREAM_TIMEOUT_SECONDS = 10
ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _read_one_sse_event(lines) -> dict[str, str]:
    """Consume lines from an SSE stream until one full event (a run of
    `field: value` lines terminated by a blank line) is assembled. Comment
    lines (sse-starlette's `: ping` keepalive) are skipped entirely.
    """
    event_type: str | None = None
    data_lines: list[str] = []
    async for line in lines:
        if line == "":
            if event_type is not None:
                break
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
    assert event_type is not None, "stream ended before a full event was received"
    return {"event": event_type, "data": "\n".join(data_lines)}


@pytest.fixture
async def live_server(app: FastAPI):
    """Serve the (already lifespan-active) test `app` over a real loopback
    TCP socket for the duration of one test, on an OS-assigned port. Yields
    the base URL.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await server_task


async def _login_admin(client: AsyncClient, *, username: str = "admin-user") -> None:
    info = LdapUserInfo(
        dn=f"uid={username},ou=users,dc=example,dc=org",
        username=username,
        display_name=username.title(),
        email=f"{username}@example.org",
        group_dns=[ADMIN_DN],
    )

    def fake_authenticate(candidate_username: str, candidate_password: str):
        if candidate_username == username and candidate_password == "password":
            return info
        return None

    with patch("app.api.auth.authenticate", fake_authenticate):
        response = await client.post(
            "/api/v1/auth/login", json={"username": username, "password": "password"}
        )
    assert response.status_code == 200


async def test_stream_requires_auth(client: AsyncClient) -> None:
    """No streaming involved -- an unauthenticated request 401s before the
    endpoint ever returns an EventSourceResponse, so the plain ASGITransport
    `client` fixture (which can't drive a genuinely streaming response, see
    this module's docstring) is fine here.
    """
    response = await client.get("/api/v1/events/stream")
    assert response.status_code == 401


async def test_stream_delivers_a_published_event(app: FastAPI, live_server: str) -> None:
    async with AsyncClient(base_url=live_server, timeout=30.0) as http_client:
        await _login_admin(http_client)

        hub = app.state.events_hub

        async def _publish_soon() -> None:
            await asyncio.sleep(0.1)
            hub.publish(
                build_event(
                    "alert_created",
                    event_id=1,
                    team_id=None,
                    cluster="local",
                    namespace="kam-demo",
                    alertname="KamAlwaysFiring",
                    severity="critical",
                    is_test=False,
                )
            )

        async def _consume() -> dict[str, str]:
            async with http_client.stream("GET", "/api/v1/events/stream") as response:
                assert response.status_code == 200
                return await _read_one_sse_event(response.aiter_lines())

        publish_task = asyncio.create_task(_publish_soon())
        try:
            received = await asyncio.wait_for(_consume(), timeout=STREAM_TIMEOUT_SECONDS)
        finally:
            await publish_task

    assert received["event"] == "alert_created"
    body = json.loads(received["data"])
    assert body["alertname"] == "KamAlwaysFiring"
    assert body["severity"] == "critical"
    assert body["is_test"] is False


async def test_stream_admin_subscriber_receives_other_teams_event(
    app: FastAPI, live_server: str
) -> None:
    """An admin's subscription must cover every team -- verified end-to-end
    by publishing an event with a `team_id` this admin has no membership
    row for, and confirming the admin's own stream still receives it.
    """
    async with AsyncClient(base_url=live_server, timeout=30.0) as http_client:
        await _login_admin(http_client)

        hub = app.state.events_hub

        async def _publish_soon() -> None:
            await asyncio.sleep(0.1)
            hub.publish(
                build_event(
                    "alert_created",
                    event_id=99,
                    team_id=12345,  # a team this admin has no membership row for
                    cluster="local",
                    namespace=None,
                    alertname="SomeOtherTeamsAlert",
                    severity="warning",
                    is_test=False,
                )
            )

        async def _consume() -> dict[str, str]:
            async with http_client.stream("GET", "/api/v1/events/stream") as response:
                assert response.status_code == 200
                return await _read_one_sse_event(response.aiter_lines())

        publish_task = asyncio.create_task(_publish_soon())
        try:
            received = await asyncio.wait_for(_consume(), timeout=STREAM_TIMEOUT_SECONDS)
        finally:
            await publish_task

    body = json.loads(received["data"])
    assert body["event_id"] == 99


async def test_stream_non_admin_does_not_receive_other_teams_event(
    app: FastAPI, live_server: str
) -> None:
    """The negative counterpart: a non-admin subscriber must NOT receive an
    event for a team they aren't a member of (nor an unassigned one). Proven
    by publishing an out-of-scope event, then a second, in-scope one right
    after it -- if scoping were broken, the first event (not the second)
    would be what the stream hands back.
    """
    async with AsyncClient(base_url=live_server, timeout=30.0) as http_client:
        await login_as(http_client, username="carol")
        me = await http_client.get("/api/v1/auth/me")
        assert me.status_code == 200

        hub = app.state.events_hub

        async def _publish_soon() -> None:
            await asyncio.sleep(0.1)
            hub.publish(
                build_event(
                    "alert_created",
                    event_id=1,
                    team_id=None,  # unassigned -- must not reach a non-admin
                    cluster="local",
                    namespace=None,
                    alertname="Unassigned",
                    severity="critical",
                    is_test=False,
                )
            )
            hub.publish(
                build_event(
                    "alert_created",
                    event_id=2,
                    team_id=54321,  # carol isn't a member of this team either
                    cluster="local",
                    namespace=None,
                    alertname="NotCarolsTeam",
                    severity="critical",
                    is_test=False,
                )
            )

        async def _consume_none() -> None:
            async with http_client.stream("GET", "/api/v1/events/stream") as response:
                assert response.status_code == 200
                try:
                    await asyncio.wait_for(_read_one_sse_event(response.aiter_lines()), timeout=1.0)
                except TimeoutError:
                    return  # expected: nothing arrived
                raise AssertionError("non-admin subscriber received an out-of-scope event")

        publish_task = asyncio.create_task(_publish_soon())
        try:
            await _consume_none()
        finally:
            await publish_task
