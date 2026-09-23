"""API tests for app/api/admin_settings.py: the retention settings
GET/PUT and the on-demand purge trigger.
"""

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.audit import AuditLog
from app.services.retention import SETTING_DEFAULTS
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def test_get_settings_requires_admin(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    resp = await client.get("/api/v1/admin/settings")
    assert resp.status_code == 403


async def test_get_settings_returns_defaults_when_unset(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    resp = await client.get("/api/v1/admin/settings")
    assert resp.status_code == 200
    assert resp.json() == SETTING_DEFAULTS


async def test_put_settings_rejects_unknown_key(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    resp = await client.put(
        "/api/v1/admin/settings", json={"values": {"retention.nonsense": 5}}
    )
    assert resp.status_code == 422


async def test_put_settings_rejects_value_below_one(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    resp = await client.put(
        "/api/v1/admin/settings", json={"values": {"retention.alert_events_days": 0}}
    )
    assert resp.status_code == 422


async def test_put_settings_updates_and_audits(app, client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.put(
        "/api/v1/admin/settings", json={"values": {"retention.alert_events_days": 120}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["retention.alert_events_days"] == 120
    # Untouched keys keep reporting their (default) effective value.
    assert body["retention.audit_log_days"] == SETTING_DEFAULTS["retention.audit_log_days"]

    get_resp = await client.get("/api/v1/admin/settings")
    assert get_resp.json()["retention.alert_events_days"] == 120

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "settings.update"))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].detail == {"retention.alert_events_days": 120}


async def test_run_retention_purge_requires_admin(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    resp = await client.post("/api/v1/admin/retention/purge")
    assert resp.status_code == 403


async def test_run_retention_purge_deletes_old_rows_and_returns_summary(
    app, client: AsyncClient
) -> None:
    async with db_module.async_session_factory() as session:
        old = AuditLog(
            user_id=None, team_id=None, action="test.old", object_type="x", object_ref="1",
            created_at=datetime.now(UTC) - timedelta(days=400),
        )
        session.add(old)
        await session.commit()

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    alice_id = (await client.get("/api/v1/auth/me")).json()["id"]

    resp = await client.post("/api/v1/admin/retention/purge")
    assert resp.status_code == 200
    summary = resp.json()["summary"]
    assert summary["audit_logs"] >= 1

    async with db_module.async_session_factory() as session:
        purge_rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "retention.purge"))
        ).scalars().all()
        assert len(purge_rows) == 1
        assert purge_rows[0].user_id == alice_id
