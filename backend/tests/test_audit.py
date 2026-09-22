from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.audit import AuditLog
from app.models.user import User
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def test_login_writes_audit_row(client: AsyncClient) -> None:
    await login_as(client, username="alice")

    async with db_module.async_session_factory() as session:
        alice = (
            await session.execute(select(User).where(User.username == "alice"))
        ).scalar_one()
        rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "auth.login")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].user_id == alice.id
        assert rows[0].object_ref == "alice"


async def test_team_create_and_member_add_write_audit_rows(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    alice_id = (await client.get("/api/v1/auth/me")).json()["id"]

    created = await client.post(
        "/api/v1/teams", json={"slug": "sre", "name": "SRE"}
    )
    team_id = created.json()["id"]

    add_resp = await client.post(
        f"/api/v1/teams/{team_id}/members", json={"user_id": alice_id, "role": "owner"}
    )
    assert add_resp.status_code == 201

    async with db_module.async_session_factory() as session:
        create_rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "team.create")
            )
        ).scalars().all()
        assert len(create_rows) == 1
        assert create_rows[0].user_id == alice_id
        assert create_rows[0].team_id == team_id
        assert create_rows[0].object_ref == "sre"

        member_rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "team.member.add")
            )
        ).scalars().all()
        assert len(member_rows) == 1
        assert member_rows[0].user_id == alice_id
        assert member_rows[0].team_id == team_id
        assert member_rows[0].object_ref == "alice"
