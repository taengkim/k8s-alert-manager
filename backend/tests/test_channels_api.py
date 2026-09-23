"""API tests for channel CRUD + test-send (app/api/channels.py): RBAC,
config schema validation, Fernet-encrypted storage, and the test-send
endpoint's success/failure mapping.
"""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.audit import AuditLog
from app.models.channel import Channel
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _fresh_client(app) -> AsyncClient:
    """A second AsyncClient on the same app/db with its own cookie jar."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _create_team(name: str) -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=name, name=name.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(team_id: int, user_id: int, role: str) -> None:
    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(team_id=team_id, user_id=user_id, role=role, origin="manual")
        )
        await session.commit()


async def test_channel_types_lists_builtin_email(client: AsyncClient) -> None:
    await login_as(client, username="alice")
    resp = await client.get("/api/v1/channel-types")
    assert resp.status_code == 200

    types = resp.json()
    email_type = next(item for item in types if item["type_name"] == "email")
    assert email_type["display_name"] == "Email"
    assert "recipients" in email_type["json_schema"]["properties"]


async def test_create_channel_requires_owner(app) -> None:
    team_id = await _create_team("payments")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={
                "name": "ops-email",
                "type": "email",
                "config": {"recipients": ["ops@example.org"]},
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "email"
        assert body["enabled"] is True
        assert body["config"]["recipients"] == ["ops@example.org"]

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "other", "type": "email", "config": {"recipients": ["x@example.org"]}},
        )
        assert resp.status_code == 403

    async with await _fresh_client(app) as outsider_client:
        await login_as(outsider_client, username="dave")
        resp = await outsider_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "other2", "type": "email", "config": {"recipients": ["x@example.org"]}},
        )
        assert resp.status_code == 403


async def test_admin_bypasses_team_rbac(client: AsyncClient) -> None:
    team_id = await _create_team("admin-owned")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "admin-email", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    assert resp.status_code == 201


async def test_create_channel_unknown_type_404(client: AsyncClient) -> None:
    team_id = await _create_team("t-unknown")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "x", "type": "sms", "config": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown channel type"


async def test_create_channel_invalid_config_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-invalid")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "bad", "type": "email", "config": {"recipients": []}},
    )
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)


async def test_create_channel_encrypts_config_and_get_round_trips(client: AsyncClient) -> None:
    team_id = await _create_team("t-crypt")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={
            "name": "secret-email",
            "type": "email",
            "config": {"recipients": ["ops@example.org"]},
        },
    )
    channel_id = create_resp.json()["id"]

    async with db_module.async_session_factory() as session:
        row = await session.get(Channel, channel_id)
        assert "ops@example.org" not in row.config_encrypted
        assert "ops" not in row.config_encrypted

    get_resp = await client.get(f"/api/v1/teams/{team_id}/channels")
    assert get_resp.status_code == 200
    [item] = [c for c in get_resp.json() if c["id"] == channel_id]
    assert item["config"]["recipients"] == ["ops@example.org"]
    assert item["config"]["subject_prefix"] == "[KAM]"


async def test_patch_channel_owner_only_and_updates_fields(app) -> None:
    team_id = await _create_team("t-patch")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "c1", "type": "email", "config": {"recipients": ["a@example.org"]}},
        )
        channel_id = create_resp.json()["id"]

        patch_resp = await owner_client.patch(
            f"/api/v1/channels/{channel_id}",
            json={
                "name": "c1-renamed",
                "enabled": False,
                "config": {"recipients": ["b@example.org"]},
            },
        )
        assert patch_resp.status_code == 200
        body = patch_resp.json()
        assert body["name"] == "c1-renamed"
        assert body["enabled"] is False
        assert body["config"]["recipients"] == ["b@example.org"]

        bad_patch = await owner_client.patch(
            f"/api/v1/channels/{channel_id}", json={"config": {"recipients": []}}
        )
        assert bad_patch.status_code == 422

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.patch(
            f"/api/v1/channels/{channel_id}", json={"name": "hacked"}
        )
        assert resp.status_code == 403


async def test_delete_channel_owner_only(app) -> None:
    team_id = await _create_team("t-delete")
    channel_id: int

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={
                "name": "to-delete",
                "type": "email",
                "config": {"recipients": ["a@example.org"]},
            },
        )
        channel_id = create_resp.json()["id"]

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.delete(f"/api/v1/channels/{channel_id}")
        assert resp.status_code == 403

    async with await _fresh_client(app) as owner_client2:
        await login_as(owner_client2, username="bob")
        resp = await owner_client2.delete(f"/api/v1/channels/{channel_id}")
        assert resp.status_code == 204

    async with db_module.async_session_factory() as session:
        # Soft-deleted, not removed: the row (and its delivery history)
        # must survive -- see test_channel_soft_delete.py for the fuller
        # soft-delete behavior (list exclusion, name reuse, etc).
        channel = await session.get(Channel, channel_id)
        assert channel is not None
        assert channel.deleted_at is not None


async def test_test_endpoint_success_202_and_audit_row(client: AsyncClient) -> None:
    team_id = await _create_team("t-test-ok")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "test-ch", "type": "email", "config": {"recipients": ["ops@example.org"]}},
    )
    channel_id = create_resp.json()["id"]

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        resp = await client.post(f"/api/v1/channels/{channel_id}/test")
    assert resp.status_code == 202
    assert mock_send.await_count == 1

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "channel.test"))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].team_id == team_id
        assert rows[0].object_ref == "test-ch"


async def test_test_endpoint_member_allowed_but_failure_is_502_no_audit(app) -> None:
    team_id = await _create_team("t-test-fail")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={
                "name": "will-fail",
                "type": "email",
                "config": {"recipients": ["ops@example.org"]},
            },
        )
        channel_id = create_resp.json()["id"]

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        mock_send = AsyncMock(side_effect=OSError("connection refused"))
        with patch("app.channels.email.aiosmtplib.send", new=mock_send):
            resp = await member_client.post(f"/api/v1/channels/{channel_id}/test")
        assert resp.status_code == 502
        assert "connection refused" in resp.json()["detail"]

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "channel.test"))
        ).scalars().all()
        assert rows == []


async def test_test_endpoint_non_member_403(app) -> None:
    team_id = await _create_team("t-test-outsider")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "priv", "type": "email", "config": {"recipients": ["ops@example.org"]}},
        )
        channel_id = create_resp.json()["id"]

    async with await _fresh_client(app) as outsider_client:
        await login_as(outsider_client, username="dave")
        resp = await outsider_client.post(f"/api/v1/channels/{channel_id}/test")
        assert resp.status_code == 403
