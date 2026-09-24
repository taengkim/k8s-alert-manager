"""Tests for the Phase 20 report sweep (app/worker/scheduler.py's
claim_due_reports/dispatch_report_schedule/run_report_sweep) and its outbox
delivery branch (app/worker/outbox.py's deliver(), trigger == 'report').
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import app.db as db_module
from app.channels.base import RenderedMessage
from app.channels.email import EmailConfig
from app.channels.registry import ChannelRegistry
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.models.report import ReportSchedule
from app.models.team import Team
from app.security import encrypt_str
from app.worker.outbox import deliver
from app.worker.scheduler import (
    claim_due_reports,
    dispatch_report_schedule,
    run_report_sweep,
)


async def _create_team(session, slug: str = "sweep-team") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_channel(session, team: Team, name: str = "c1") -> Channel:
    channel = Channel(
        team_id=team.id,
        name=name,
        type="email",
        config_encrypted=encrypt_str(EmailConfig(recipients=["ops@example.org"]).model_dump_json()),
    )
    session.add(channel)
    await session.flush()
    return channel


async def _create_schedule(
    session,
    team: Team,
    *,
    name: str = "weekly",
    channels=None,
    next_run_at: datetime,
    enabled: bool = True,
) -> ReportSchedule:
    schedule = ReportSchedule(
        team_id=team.id,
        name=name,
        enabled=enabled,
        cadence="weekly",
        weekday=0,
        hour=9,
        timezone="UTC",
        next_run_at=next_run_at,
        channels=channels or [],
    )
    session.add(schedule)
    await session.flush()
    return schedule


# -- claim_due_reports ----------------------------------------------------------


async def test_claim_due_reports_claims_only_due_enabled_rows(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        now = datetime.now(UTC)

        due = await _create_schedule(session, team, name="due", next_run_at=now - timedelta(minutes=1))
        not_due = await _create_schedule(
            session, team, name="not-due", next_run_at=now + timedelta(hours=1)
        )
        disabled = await _create_schedule(
            session, team, name="disabled", next_run_at=now - timedelta(minutes=1), enabled=False
        )
        await session.commit()

        claimed = await claim_due_reports(session, "worker-1", limit=50)
        claimed_ids = {schedule_id for schedule_id, _due_at in claimed}

        assert claimed_ids == {due.id}
        assert not_due.id not in claimed_ids
        assert disabled.id not in claimed_ids


async def test_claim_due_reports_returns_pre_claim_due_at(app) -> None:
    """The (schedule_id, due_at) pair must carry the ORIGINAL next_run_at,
    not the provisional bumped value -- dispatch_report_schedule uses it as
    the report's period-computation reference."""
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        original_due = datetime.now(UTC) - timedelta(minutes=5)
        schedule = await _create_schedule(session, team, next_run_at=original_due)
        await session.commit()

        claimed = await claim_due_reports(session, "worker-1", limit=50)
        assert claimed == [(schedule.id, original_due)]

        await session.refresh(schedule)
        # Provisionally bumped forward, well past "now" -- unclaimable by a
        # concurrent worker or the very next sweep tick.
        assert schedule.next_run_at > datetime.now(UTC) + timedelta(minutes=30)


async def test_claim_due_reports_prevents_double_claim(app) -> None:
    """Once claimed, a row's provisional next_run_at bump means a second
    concurrent claim attempt (simulated here as a second call) does not
    re-claim it."""
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        await _create_schedule(session, team, next_run_at=datetime.now(UTC) - timedelta(minutes=1))
        await session.commit()

        first = await claim_due_reports(session, "worker-1", limit=50)
        second = await claim_due_reports(session, "worker-2", limit=50)

        assert len(first) == 1
        assert second == []


# -- dispatch_report_schedule -----------------------------------------------------


async def test_dispatch_report_schedule_stages_outbox_per_channel_and_advances(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "dispatch-team")
        c1 = await _create_channel(session, team, "c1")
        c2 = await _create_channel(session, team, "c2")
        due_at = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)  # a Monday
        schedule = await _create_schedule(session, team, channels=[c1, c2], next_run_at=due_at)
        await session.commit()

        await dispatch_report_schedule(schedule.id, due_at, session)

        await session.refresh(schedule)
        assert schedule.last_status == "ok"
        assert schedule.last_run_at is not None
        # Advanced past the claimed due_at to the NEXT weekly occurrence.
        assert schedule.next_run_at > due_at

        rows = (
            (await session.execute(select(NotificationOutbox).where(NotificationOutbox.trigger == "report")))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        channel_ids = {r.channel_id for r in rows}
        assert channel_ids == {c1.id, c2.id}
        for row in rows:
            assert row.alert_event_id is None
            assert row.is_digest is False
            assert row.status == "pending"
            assert row.team_id == team.id
            assert "rendered" in row.payload
            assert row.payload["schedule_id"] == schedule.id
            assert "period" in row.payload


async def test_dispatch_report_schedule_skips_deleted_channels(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "dispatch-deleted")
        active = await _create_channel(session, team, "active")
        deleted = await _create_channel(session, team, "deleted")
        deleted.deleted_at = datetime.now(UTC)
        due_at = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        schedule = await _create_schedule(session, team, channels=[active, deleted], next_run_at=due_at)
        await session.commit()

        await dispatch_report_schedule(schedule.id, due_at, session)

        rows = (
            (await session.execute(select(NotificationOutbox).where(NotificationOutbox.trigger == "report")))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].channel_id == active.id


async def test_dispatch_report_schedule_records_error_and_still_advances(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "dispatch-fail")
        channel = await _create_channel(session, team)
        due_at = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        schedule = await _create_schedule(session, team, channels=[channel], next_run_at=due_at)
        await session.commit()

        with patch(
            "app.worker.scheduler.reports_service.build_report_data",
            side_effect=RuntimeError("stats exploded"),
        ):
            await dispatch_report_schedule(schedule.id, due_at, session)

        await session.refresh(schedule)
        assert schedule.last_status is not None
        assert schedule.last_status.startswith("error:")
        assert "stats exploded" in schedule.last_status
        # Advances to the NEXT regular occurrence rather than retrying soon
        # -- see dispatch_report_schedule's docstring.
        assert schedule.next_run_at > due_at

        rows = (
            (await session.execute(select(NotificationOutbox).where(NotificationOutbox.trigger == "report")))
            .scalars()
            .all()
        )
        assert rows == []


async def test_dispatch_report_schedule_missing_row_is_noop(app) -> None:
    async with db_module.async_session_factory() as session:
        # No schedule with this id exists -- must not raise.
        await dispatch_report_schedule(999999, datetime.now(UTC), session)


# -- run_report_sweep (integration) ----------------------------------------------


async def test_run_report_sweep_claims_and_dispatches(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "sweep-integration")
        channel = await _create_channel(session, team)
        due_at = datetime(2026, 1, 12, 9, 0, tzinfo=UTC)
        await _create_schedule(session, team, channels=[channel], next_run_at=due_at - timedelta(minutes=1))
        await session.commit()

    processed = await run_report_sweep(db_module.async_session_factory, "worker-1")
    assert processed == 1

    async with db_module.async_session_factory() as session:
        schedule = (await session.execute(select(ReportSchedule))).scalar_one()
        assert schedule.last_status == "ok"
        rows = (
            (await session.execute(select(NotificationOutbox).where(NotificationOutbox.trigger == "report")))
            .scalars()
            .all()
        )
        assert len(rows) == 1


# -- outbox deliver(): trigger == 'report' ---------------------------------------


async def test_deliver_report_row_restores_rendered_message_and_calls_send_message(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "deliver-team")
        channel = await _create_channel(session, team)
        await session.flush()

        rendered = RenderedMessage(title="Weekly Report", body="events: 5", body_html="<p>5</p>")
        row = NotificationOutbox(
            alert_event_id=None,
            routing_rule_id=None,
            channel_id=channel.id,
            team_id=team.id,
            trigger="report",
            payload={"rendered": rendered.model_dump(mode="json"), "schedule_id": 1, "period": {}},
            is_digest=False,
            status="in_progress",
        )
        session.add(row)
        await session.commit()

        registry = ChannelRegistry()
        registry.discover()

        mock_send_message = AsyncMock()
        with patch("app.channels.email.EmailChannel.send_message", new=mock_send_message):
            await deliver(row, registry, session)

        await session.refresh(row)
        assert row.status == "delivered"
        mock_send_message.assert_awaited_once()
        (sent_msg,) = mock_send_message.await_args.args
        assert sent_msg.title == "Weekly Report"
        assert sent_msg.body == "events: 5"


async def test_deliver_report_row_uses_email_send_message_not_send(app) -> None:
    """A report delivery must go through send_message(), never the ordinary
    send() path -- there is no AlertNotification behind a report."""
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "deliver-team-2")
        channel = await _create_channel(session, team)
        await session.flush()

        rendered = RenderedMessage(title="R", body="B", body_html=None)
        row = NotificationOutbox(
            alert_event_id=None,
            routing_rule_id=None,
            channel_id=channel.id,
            team_id=team.id,
            trigger="report",
            payload={"rendered": rendered.model_dump(mode="json")},
            is_digest=False,
            status="in_progress",
        )
        session.add(row)
        await session.commit()

        registry = ChannelRegistry()
        registry.discover()

        mock_send = AsyncMock()
        mock_aiosmtplib_send = AsyncMock(return_value=({}, "OK"))
        with (
            patch("app.channels.email.EmailChannel.send", new=mock_send),
            patch("app.channels.email.aiosmtplib.send", new=mock_aiosmtplib_send),
        ):
            await deliver(row, registry, session)

        mock_send.assert_not_awaited()
        mock_aiosmtplib_send.assert_awaited_once()
