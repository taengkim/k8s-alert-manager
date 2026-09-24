"""API tests for app/api/reports.py: RBAC, validation (cadence/weekday/hour/
timezone/channel ownership/template kind), run-now (no schedule advance),
preview (no state change at all), and next_run_at computation on save.
"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.models.report import ReportSchedule
from app.models.team import Team, TeamMembership
from app.models.template import MessageTemplate
from app.security import encrypt_str
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _fresh_client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _create_team(slug: str) -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(team_id: int, user_id: int, role: str) -> None:
    async with db_module.async_session_factory() as session:
        session.add(TeamMembership(team_id=team_id, user_id=user_id, role=role, origin="manual"))
        await session.commit()


async def _create_channel(team_id: int, name: str = "email-1") -> int:
    async with db_module.async_session_factory() as session:
        channel = Channel(
            team_id=team_id,
            name=name,
            type="email",
            config_encrypted=encrypt_str('{"recipients": ["ops@example.org"]}'),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel.id


async def _create_template(team_id: int, *, kind: str = "report", name: str = "tpl-1") -> int:
    async with db_module.async_session_factory() as session:
        template = MessageTemplate(
            team_id=team_id,
            name=name,
            kind=kind,
            title_template="[KAM] {{ team }}" if kind == "report" else "{{ alertname }}",
            body_template="body",
        )
        session.add(template)
        await session.commit()
        await session.refresh(template)
        return template.id


async def _create_schedule(
    team_id: int, channel_id: int, *, name: str = "s1", next_run_at: datetime | None = None
) -> int:
    async with db_module.async_session_factory() as session:
        result = await session.get(Channel, channel_id)
        schedule = ReportSchedule(
            team_id=team_id,
            name=name,
            cadence="weekly",
            weekday=0,
            hour=9,
            timezone="UTC",
            next_run_at=next_run_at or datetime.now(UTC),
            channels=[result],
        )
        session.add(schedule)
        await session.commit()
        await session.refresh(schedule)
        return schedule.id


@asynccontextmanager
async def _owner_client(app, team_id: int, username: str):
    # Every request (login, /auth/me, and whatever the caller does inside
    # the `async with` block) must happen AFTER __aenter__ -- httpx 0.28's
    # AsyncClient tracks open/closed state and raises if a request is sent
    # before the client is entered as a context manager, then entered again.
    async with await _fresh_client(app) as client:
        await login_as(client, username=username)
        user_id = (await client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, user_id, "owner")
        yield client


@asynccontextmanager
async def _member_client(app, team_id: int, username: str):
    async with await _fresh_client(app) as client:
        await login_as(client, username=username)
        user_id = (await client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, user_id, "member")
        yield client


# -- create: RBAC + validation ----------------------------------------------------


async def test_create_report_schedule_requires_owner(app) -> None:
    team_id = await _create_team("rep-owner")
    channel_id = await _create_channel(team_id)

    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={
                "name": "weekly-report",
                "cadence": "weekly",
                "weekday": 0,
                "hour": 9,
                "timezone": "UTC",
                "channel_ids": [channel_id],
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "weekly-report"
        assert body["channel_ids"] == [channel_id]
        assert body["next_run_at"] is not None
        assert body["last_run_at"] is None

    async with _member_client(app, team_id, "carol") as member:
        resp = await member.post(
            f"/api/v1/teams/{team_id}/reports",
            json={"name": "x", "cadence": "daily", "hour": 9, "channel_ids": [channel_id]},
        )
        assert resp.status_code == 403


async def test_create_report_schedule_requires_channels(app) -> None:
    team_id = await _create_team("rep-nochannel")
    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={"name": "x", "cadence": "daily", "hour": 9, "channel_ids": []},
        )
        assert resp.status_code == 422


async def test_create_report_schedule_weekly_requires_weekday(app) -> None:
    team_id = await _create_team("rep-weekday-req")
    channel_id = await _create_channel(team_id)
    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={"name": "x", "cadence": "weekly", "hour": 9, "channel_ids": [channel_id]},
        )
        assert resp.status_code == 422


async def test_create_report_schedule_daily_rejects_weekday(app) -> None:
    team_id = await _create_team("rep-daily-weekday")
    channel_id = await _create_channel(team_id)
    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={"name": "x", "cadence": "daily", "weekday": 2, "hour": 9, "channel_ids": [channel_id]},
        )
        assert resp.status_code == 422


async def test_create_report_schedule_invalid_hour_422(app) -> None:
    team_id = await _create_team("rep-bad-hour")
    channel_id = await _create_channel(team_id)
    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={"name": "x", "cadence": "daily", "hour": 25, "channel_ids": [channel_id]},
        )
        assert resp.status_code == 422


async def test_create_report_schedule_invalid_timezone_422(app) -> None:
    team_id = await _create_team("rep-bad-tz")
    channel_id = await _create_channel(team_id)
    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={
                "name": "x",
                "cadence": "daily",
                "hour": 9,
                "timezone": "Not/AZone",
                "channel_ids": [channel_id],
            },
        )
        assert resp.status_code == 422


async def test_create_report_schedule_rejects_foreign_team_channel(app) -> None:
    team_id = await _create_team("rep-own-team")
    other_team_id = await _create_team("rep-other-team")
    foreign_channel_id = await _create_channel(other_team_id)

    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={"name": "x", "cadence": "daily", "hour": 9, "channel_ids": [foreign_channel_id]},
        )
        assert resp.status_code == 422


async def test_create_report_schedule_rejects_unknown_channel_id(app) -> None:
    team_id = await _create_team("rep-unknown-channel")
    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={"name": "x", "cadence": "daily", "hour": 9, "channel_ids": [999999]},
        )
        assert resp.status_code == 422


async def test_create_report_schedule_rejects_non_report_kind_template(app) -> None:
    team_id = await _create_team("rep-tpl-kind")
    channel_id = await _create_channel(team_id)
    alert_template_id = await _create_template(team_id, kind="alert")

    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={
                "name": "x",
                "cadence": "daily",
                "hour": 9,
                "channel_ids": [channel_id],
                "template_id": alert_template_id,
            },
        )
        assert resp.status_code == 422


async def test_create_report_schedule_accepts_report_kind_template(app) -> None:
    team_id = await _create_team("rep-tpl-good")
    channel_id = await _create_channel(team_id)
    report_template_id = await _create_template(team_id, kind="report")

    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.post(
            f"/api/v1/teams/{team_id}/reports",
            json={
                "name": "x",
                "cadence": "daily",
                "hour": 9,
                "channel_ids": [channel_id],
                "template_id": report_template_id,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["template_id"] == report_template_id


# -- list / get -------------------------------------------------------------------


async def test_list_report_schedules_member_allowed(app) -> None:
    team_id = await _create_team("rep-list")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _member_client(app, team_id, "dave") as member:
        resp = await member.get(f"/api/v1/teams/{team_id}/reports")
        assert resp.status_code == 200
        [item] = resp.json()
        assert item["id"] == schedule_id


async def test_get_report_schedule_non_member_403(app) -> None:
    team_id = await _create_team("rep-get-priv")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with await _fresh_client(app) as outsider:
        await login_as(outsider, username="eve")
        resp = await outsider.get(f"/api/v1/reports/{schedule_id}")
        assert resp.status_code == 403


async def test_get_report_schedule_not_found_404(app) -> None:
    team_id = await _create_team("rep-404")
    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.get("/api/v1/reports/999999")
        assert resp.status_code == 404


# -- update -------------------------------------------------------------------


async def test_update_report_schedule_owner_only_and_recomputes_next_run(app) -> None:
    team_id = await _create_team("rep-update")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _owner_client(app, team_id, "bob") as owner:
        before = (await owner.get(f"/api/v1/reports/{schedule_id}")).json()

        resp = await owner.patch(f"/api/v1/reports/{schedule_id}", json={"hour": 14})
        assert resp.status_code == 200
        after = resp.json()
        assert after["hour"] == 14
        assert after["next_run_at"] != before["next_run_at"]

    async with _member_client(app, team_id, "carol") as member:
        resp = await member.patch(f"/api/v1/reports/{schedule_id}", json={"hour": 3})
        assert resp.status_code == 403


async def test_update_report_schedule_switch_cadence_clears_weekday(app) -> None:
    team_id = await _create_team("rep-update-cadence")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _owner_client(app, team_id, "bob") as owner:
        # Switching to daily while weekday is still set (from the weekly
        # fixture) must be rejected -- the final merged state (cadence=daily,
        # weekday=0) is invalid even though this PATCH itself didn't touch
        # weekday.
        resp = await owner.patch(f"/api/v1/reports/{schedule_id}", json={"cadence": "daily"})
        assert resp.status_code == 422

        resp = await owner.patch(
            f"/api/v1/reports/{schedule_id}", json={"cadence": "daily", "weekday": None}
        )
        assert resp.status_code == 200
        assert resp.json()["cadence"] == "daily"
        assert resp.json()["weekday"] is None


async def test_update_report_schedule_channel_ids_replaces_and_validates_ownership(app) -> None:
    team_id = await _create_team("rep-update-channels")
    other_team_id = await _create_team("rep-update-channels-other")
    channel_id = await _create_channel(team_id)
    other_channel_id = await _create_channel(team_id, "c2")
    foreign_channel_id = await _create_channel(other_team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.patch(
            f"/api/v1/reports/{schedule_id}", json={"channel_ids": [foreign_channel_id]}
        )
        assert resp.status_code == 422

        resp = await owner.patch(
            f"/api/v1/reports/{schedule_id}", json={"channel_ids": [other_channel_id]}
        )
        assert resp.status_code == 200
        assert resp.json()["channel_ids"] == [other_channel_id]


# -- delete -------------------------------------------------------------------


async def test_delete_report_schedule_owner_only(app) -> None:
    team_id = await _create_team("rep-delete")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _member_client(app, team_id, "carol") as member:
        resp = await member.delete(f"/api/v1/reports/{schedule_id}")
        assert resp.status_code == 403

    async with _owner_client(app, team_id, "bob") as owner:
        resp = await owner.delete(f"/api/v1/reports/{schedule_id}")
        assert resp.status_code == 204

        resp = await owner.get(f"/api/v1/reports/{schedule_id}")
        assert resp.status_code == 404


# -- run-now: no advance, records last_run/status ----------------------------------


async def test_run_now_queues_outbox_and_does_not_advance_schedule(app) -> None:
    team_id = await _create_team("rep-run-now")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _owner_client(app, team_id, "bob") as owner:
        before = (await owner.get(f"/api/v1/reports/{schedule_id}")).json()

        resp = await owner.post(f"/api/v1/reports/{schedule_id}/run-now")
        assert resp.status_code == 200
        assert resp.json() == {"queued_channels": 1}

        after = (await owner.get(f"/api/v1/reports/{schedule_id}")).json()
        assert after["next_run_at"] == before["next_run_at"]
        assert after["last_status"] == "ok"
        assert after["last_run_at"] is not None

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(NotificationOutbox))
        ).scalars().all()
        report_rows = [r for r in rows if r.trigger == "report"]
        assert len(report_rows) == 1


async def test_run_now_member_forbidden(app) -> None:
    team_id = await _create_team("rep-run-now-403")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _member_client(app, team_id, "carol") as member:
        resp = await member.post(f"/api/v1/reports/{schedule_id}/run-now")
        assert resp.status_code == 403


async def test_run_now_failure_records_error_status_and_returns_502(app) -> None:
    team_id = await _create_team("rep-run-now-fail")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _owner_client(app, team_id, "bob") as owner:
        with patch(
            "app.api.reports.reports_service.build_report_data",
            side_effect=RuntimeError("stats down"),
        ):
            resp = await owner.post(f"/api/v1/reports/{schedule_id}/run-now")
        assert resp.status_code == 502

        after = (await owner.get(f"/api/v1/reports/{schedule_id}")).json()
        assert after["last_status"].startswith("error:")
        assert "stats down" in after["last_status"]


# -- preview: no delivery, no state change ------------------------------------------


async def test_preview_report_does_not_deliver_or_change_state(app) -> None:
    team_id = await _create_team("rep-preview")
    channel_id = await _create_channel(team_id)
    schedule_id = await _create_schedule(team_id, channel_id)

    async with _member_client(app, team_id, "dave") as member:
        before = (await member.get(f"/api/v1/reports/{schedule_id}")).json()

        resp = await member.get(f"/api/v1/reports/{schedule_id}/preview")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"title", "body", "body_html"}
        assert body["title"]
        assert body["body"]

        after = (await member.get(f"/api/v1/reports/{schedule_id}")).json()
        assert after["next_run_at"] == before["next_run_at"]
        assert after["last_run_at"] == before["last_run_at"]
        assert after["last_status"] == before["last_status"]

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(NotificationOutbox))
        ).scalars().all()
        assert [r for r in rows if r.trigger == "report"] == []
