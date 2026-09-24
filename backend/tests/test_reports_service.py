"""Tests for app/services/reports.py: DST-safe next-run computation, period
math (the immediately-preceding cadence period), and report rendering
(custom/default/fallback).
"""

from datetime import UTC, datetime

import pytest

import app.db as db_module
from app.models.report import ReportSchedule
from app.models.team import Team
from app.models.template import MessageTemplate
from app.services import reports as reports_service
from app.services.reports import (
    ReportData,
    build_report_data,
    compute_next_run,
    compute_period,
    render_report,
)


async def _create_team(session, slug: str = "reports-team") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


def _make_data(**overrides) -> ReportData:
    base = ReportData(
        team="Platform",
        team_slug="platform",
        period_start=datetime(2026, 1, 5, tzinfo=UTC),
        period_end=datetime(2026, 1, 12, tzinfo=UTC),
        timezone="UTC",
        summary={
            "firing_now": 0,
            "events_in_range": 10,
            "delivered_in_range": 9,
            "failed_or_dead_in_range": 1,
        },
        top_alerts=[{"alertname": "HighCpu", "count": 5, "receive_total": 5}],
        volume=[],
        breakdowns={"severity": [], "namespace": []},
        response_times={
            "mtta_seconds": None,
            "mttr_seconds": None,
            "acked_count": 0,
            "resolved_count": 0,
        },
        delta_pct=12.5,
    )
    base.update(overrides)  # type: ignore[arg-type]
    return base


# -- compute_next_run: daily/weekly/monthly, plain UTC -------------------------


def test_compute_next_run_daily_same_day_if_before_hour() -> None:
    after = datetime(2026, 1, 5, 3, 0, tzinfo=UTC)  # 03:00
    next_run = compute_next_run(cadence="daily", weekday=None, hour=9, timezone="UTC", after=after)
    assert next_run == datetime(2026, 1, 5, 9, 0, tzinfo=UTC)


def test_compute_next_run_daily_rolls_to_tomorrow_if_past_hour() -> None:
    after = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)  # already past 09:00
    next_run = compute_next_run(cadence="daily", weekday=None, hour=9, timezone="UTC", after=after)
    assert next_run == datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


def test_compute_next_run_daily_exact_boundary_rolls_forward() -> None:
    """Strictly AFTER `after`, not at-or-after -- a schedule due exactly now
    must advance to the NEXT occurrence, not stay pinned on the instant that
    just fired."""
    after = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    next_run = compute_next_run(cadence="daily", weekday=None, hour=9, timezone="UTC", after=after)
    assert next_run == datetime(2026, 1, 6, 9, 0, tzinfo=UTC)


def test_compute_next_run_weekly_finds_next_matching_weekday() -> None:
    # 2026-01-05 is a Monday.
    after = datetime(2026, 1, 5, 3, 0, tzinfo=UTC)
    next_run = compute_next_run(cadence="weekly", weekday=2, hour=9, timezone="UTC", after=after)
    # weekday=2 -> Wednesday, same week.
    assert next_run == datetime(2026, 1, 7, 9, 0, tzinfo=UTC)


def test_compute_next_run_weekly_same_weekday_rolls_to_next_week() -> None:
    # 2026-01-05 is a Monday; requesting weekday=0 (Monday) after 09:00 must
    # roll a full 7 days forward, not "later today".
    after = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    next_run = compute_next_run(cadence="weekly", weekday=0, hour=9, timezone="UTC", after=after)
    assert next_run == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)


def test_compute_next_run_weekly_requires_weekday() -> None:
    with pytest.raises(ValueError):
        compute_next_run(cadence="weekly", weekday=None, hour=9, timezone="UTC", after=datetime.now(UTC))


def test_compute_next_run_monthly_fires_on_first() -> None:
    after = datetime(2026, 1, 15, 3, 0, tzinfo=UTC)
    next_run = compute_next_run(cadence="monthly", weekday=None, hour=9, timezone="UTC", after=after)
    assert next_run == datetime(2026, 2, 1, 9, 0, tzinfo=UTC)


def test_compute_next_run_monthly_rolls_year_boundary() -> None:
    after = datetime(2026, 12, 15, 3, 0, tzinfo=UTC)
    next_run = compute_next_run(cadence="monthly", weekday=None, hour=9, timezone="UTC", after=after)
    assert next_run == datetime(2027, 1, 1, 9, 0, tzinfo=UTC)


def test_compute_next_run_unknown_cadence_raises() -> None:
    with pytest.raises(ValueError):
        compute_next_run(cadence="yearly", weekday=None, hour=9, timezone="UTC", after=datetime.now(UTC))


# -- compute_next_run: timezone-aware (non-DST) --------------------------------


def test_compute_next_run_respects_non_utc_timezone() -> None:
    # Asia/Seoul is UTC+9, no DST. 09:00 KST == 00:00 UTC.
    after = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # exactly 09:00 KST Monday
    next_run = compute_next_run(
        cadence="daily", weekday=None, hour=9, timezone="Asia/Seoul", after=after
    )
    # Strictly after -- rolls to the next day at 09:00 KST == 00:00 UTC the next day.
    assert next_run == datetime(2026, 1, 6, 0, 0, tzinfo=UTC)
    assert next_run.astimezone(reports_service.ZoneInfo("Asia/Seoul")).hour == 9


# -- compute_next_run: DST safety (America/New_York, March transition) --------
#
# 2026 DST spring-forward in the US is 2026-03-08 (02:00 -> 03:00 local,
# EST/UTC-5 -> EDT/UTC-4). A weekly Monday-09:00 schedule must keep firing at
# 09:00 *local* time on both sides of that transition, even though the UTC
# instant that corresponds to shifts by exactly one hour.


@pytest.mark.parametrize(
    ("after_utc", "expected_utc"),
    [
        # Before the transition (asked on a Tuesday): next Monday
        # (2026-03-02) 09:00 EST = 14:00 UTC.
        (datetime(2026, 2, 24, 12, 0, tzinfo=UTC), datetime(2026, 3, 2, 14, 0, tzinfo=UTC)),
        # Straddling the transition (asked on the Tuesday right after the
        # first Monday but before the switch): next Monday (2026-03-09,
        # AFTER the 03-08 spring-forward) 09:00 EDT = 13:00 UTC -- note the
        # UTC hour shifts from 14 to 13 even though local wall-clock stays 09:00.
        (datetime(2026, 3, 3, 12, 0, tzinfo=UTC), datetime(2026, 3, 9, 13, 0, tzinfo=UTC)),
        # After the transition: still 09:00 EDT = 13:00 UTC the following Monday.
        (datetime(2026, 3, 10, 12, 0, tzinfo=UTC), datetime(2026, 3, 16, 13, 0, tzinfo=UTC)),
    ],
)
def test_compute_next_run_weekly_dst_safe_new_york(after_utc, expected_utc) -> None:
    next_run = compute_next_run(
        cadence="weekly", weekday=0, hour=9, timezone="America/New_York", after=after_utc
    )
    assert next_run == expected_utc
    local = next_run.astimezone(reports_service.ZoneInfo("America/New_York"))
    assert local.hour == 9
    assert local.weekday() == 0


@pytest.mark.parametrize("cadence", ["daily", "weekly", "monthly"])
@pytest.mark.parametrize(
    "after_utc",
    [
        datetime(2026, 2, 20, 0, 0, tzinfo=UTC),
        datetime(2026, 3, 8, 0, 0, tzinfo=UTC),  # the DST transition date itself
        datetime(2026, 3, 15, 0, 0, tzinfo=UTC),
    ],
)
def test_compute_next_run_local_hour_always_matches_across_dst(cadence, after_utc) -> None:
    """Regardless of cadence or which side of the DST boundary `after` falls
    on, the computed next_run's own local wall-clock hour must always be the
    schedule's configured hour -- the whole point of computing candidates in
    local time via zoneinfo rather than adding a fixed UTC timedelta.
    """
    weekday = 0 if cadence == "weekly" else None
    next_run = compute_next_run(
        cadence=cadence, weekday=weekday, hour=9, timezone="America/New_York", after=after_utc
    )
    local = next_run.astimezone(reports_service.ZoneInfo("America/New_York"))
    assert local.hour == 9
    assert next_run > after_utc


# -- compute_period -------------------------------------------------------------


def test_compute_period_daily_is_previous_full_day() -> None:
    reference = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    start, end = compute_period(cadence="daily", timezone="UTC", reference=reference)
    assert start == datetime(2026, 1, 14, tzinfo=UTC)
    assert end == datetime(2026, 1, 15, tzinfo=UTC)


def test_compute_period_weekly_is_last_monday_to_sunday() -> None:
    # 2026-01-15 is a Thursday in week-of-2026-01-12 (Monday).
    reference = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    start, end = compute_period(cadence="weekly", timezone="UTC", reference=reference)
    assert start == datetime(2026, 1, 5, tzinfo=UTC)  # Monday of the previous week
    assert end == datetime(2026, 1, 12, tzinfo=UTC)  # Monday of the current week
    assert (end - start).days == 7


def test_compute_period_weekly_reference_exactly_on_monday() -> None:
    # Reference itself being the week boundary must still describe the FULL
    # preceding week, not a zero-length or off-by-one period.
    reference = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)  # a Monday, 00:00
    start, end = compute_period(cadence="weekly", timezone="UTC", reference=reference)
    assert start == datetime(2026, 1, 5, tzinfo=UTC)
    assert end == datetime(2026, 1, 12, tzinfo=UTC)


def test_compute_period_monthly_is_previous_calendar_month() -> None:
    reference = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)
    start, end = compute_period(cadence="monthly", timezone="UTC", reference=reference)
    assert start == datetime(2026, 2, 1, tzinfo=UTC)
    assert end == datetime(2026, 3, 1, tzinfo=UTC)


def test_compute_period_monthly_year_boundary() -> None:
    reference = datetime(2026, 1, 10, 14, 30, tzinfo=UTC)
    start, end = compute_period(cadence="monthly", timezone="UTC", reference=reference)
    assert start == datetime(2025, 12, 1, tzinfo=UTC)
    assert end == datetime(2026, 1, 1, tzinfo=UTC)


def test_compute_period_respects_local_timezone_boundary() -> None:
    """A UTC reference that's already past local midnight in a +9 timezone
    must use the LOCAL day boundary, not the UTC one -- e.g. 2026-01-15
    00:30 UTC is already 2026-01-15 09:30 in Asia/Seoul, so "yesterday"
    locally is 2026-01-14, not 2026-01-13 (which a naive UTC-only
    calculation might produce if it didn't convert to local time first)."""
    reference = datetime(2026, 1, 15, 0, 30, tzinfo=UTC)
    start, end = compute_period(cadence="daily", timezone="Asia/Seoul", reference=reference)
    tz = reports_service.ZoneInfo("Asia/Seoul")
    assert start.astimezone(tz) == datetime(2026, 1, 14, tzinfo=tz)
    assert end.astimezone(tz) == datetime(2026, 1, 15, tzinfo=tz)


def test_compute_period_unknown_cadence_raises() -> None:
    with pytest.raises(ValueError):
        compute_period(cadence="yearly", timezone="UTC", reference=datetime.now(UTC))


# -- build_report_data -----------------------------------------------------------


async def test_build_report_data_computes_delta_pct(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        schedule = ReportSchedule(
            team_id=team.id,
            name="weekly",
            cadence="weekly",
            weekday=0,
            hour=9,
            timezone="UTC",
            next_run_at=datetime(2026, 1, 12, 9, tzinfo=UTC),
        )
        session.add(schedule)
        await session.flush()
        await session.commit()

        # Reference falls in the week of 2026-01-12 (Mon) -- previous period
        # is [2026-01-05, 2026-01-12), with zero events seeded, so delta_pct
        # against a zero previous baseline must be None (undefined), not an
        # artificial 0%/inf.
        data = await build_report_data(
            session, schedule, reference=datetime(2026, 1, 15, tzinfo=UTC)
        )
        assert data["period_start"] == datetime(2026, 1, 5, tzinfo=UTC)
        assert data["period_end"] == datetime(2026, 1, 12, tzinfo=UTC)
        assert data["team"] == team.name
        assert data["delta_pct"] is None
        assert data["summary"]["events_in_range"] == 0


# -- render_report: custom / default / fallback ----------------------------------


async def test_render_report_uses_default_template_when_none_assigned(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "render-default")
        schedule = ReportSchedule(
            team_id=team.id, name="s", cadence="weekly", weekday=0, hour=9, timezone="UTC",
            next_run_at=datetime.now(UTC),
        )
        session.add(schedule)
        await session.flush()

        data = _make_data()
        message = await render_report(session, schedule, data)
        assert "Platform" in message.title
        assert "2026-01-05" in message.title
        assert "HighCpu" in message.body
        assert "<table" in message.body_html


async def test_render_report_uses_custom_report_kind_template(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "render-custom")
        template = MessageTemplate(
            team_id=team.id,
            name="custom-report",
            kind="report",
            title_template="Custom: {{ team }}",
            body_template="events={{ summary.events_in_range }}",
        )
        session.add(template)
        await session.flush()

        schedule = ReportSchedule(
            team_id=team.id, name="s", cadence="weekly", weekday=0, hour=9, timezone="UTC",
            template_id=template.id, next_run_at=datetime.now(UTC),
        )
        session.add(schedule)
        await session.flush()

        data = _make_data(team="CustomTeam")
        message = await render_report(session, schedule, data)
        assert message.title == "Custom: CustomTeam"
        assert message.body == "events=10"


async def test_render_report_falls_back_when_template_kind_changed(app) -> None:
    """A template originally kind='report' can later be edited to
    kind='alert' via PUT /templates/{id} while a schedule still references
    it -- render_report must treat that the same as "no custom template"
    rather than rendering an alert-shaped template against report data."""
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "render-kind-changed")
        template = MessageTemplate(
            team_id=team.id,
            name="was-report",
            kind="alert",  # changed since the schedule was saved
            title_template="Custom: {{ team }}",
            body_template="ok",
        )
        session.add(template)
        await session.flush()

        schedule = ReportSchedule(
            team_id=team.id, name="s", cadence="weekly", weekday=0, hour=9, timezone="UTC",
            template_id=template.id, next_run_at=datetime.now(UTC),
        )
        session.add(schedule)
        await session.flush()

        data = _make_data(team="Platform")
        message = await render_report(session, schedule, data)
        # Falls through to the default report template, not the (now
        # alert-kind) custom one.
        assert message.title != "Custom: Platform"
        assert "Platform" in message.title
        assert "2026-01-05" in message.title


async def test_render_report_falls_back_on_broken_custom_template(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "render-broken")
        template = MessageTemplate(
            team_id=team.id,
            name="broken-report",
            kind="report",
            title_template="{{ unterminated",
            body_template="ok",
        )
        session.add(template)
        await session.flush()

        schedule = ReportSchedule(
            team_id=team.id, name="s", cadence="weekly", weekday=0, hour=9, timezone="UTC",
            template_id=template.id, next_run_at=datetime.now(UTC),
        )
        session.add(schedule)
        await session.flush()

        data = _make_data(team="Platform")
        message = await render_report(session, schedule, data)
        # Rendered via DEFAULT_REPORT_TEMPLATES instead -- never raises, and
        # never delivers a broken/empty report.
        assert "Platform" in message.title
        assert "2026-01-05" in message.title


def test_report_template_variables_cover_report_data_fields() -> None:
    names = {v["name"] for v in reports_service.report_template_variables()}
    assert "team" in names
    assert "summary.events_in_range" in names
    assert "top_alerts" in names
    assert "delta_pct" in names
    assert "now()" in names
