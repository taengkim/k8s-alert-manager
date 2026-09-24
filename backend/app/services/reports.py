"""Weekly/daily/monthly report generation (Phase 20): builds a `ReportData`
snapshot over a team's own alert activity for the immediately-preceding
cadence period, and renders it into a `RenderedMessage` a channel can
deliver.

Reuses `app.services.stats` unchanged (same function signatures as the
Stats dashboard, Phase 19) -- see that module's docstring for what it does
and does NOT cover: notably, it excludes Phase 14's AlertShare cross-team
widening, so a report describes a team's own incidents only, same as the
Stats page. `cluster_ids` is always `None` here (a report is team-wide, not
cluster-scoped) -- nothing in this phase's brief calls for a per-cluster
report.

Delivery itself (claiming due schedules, staging outbox rows, advancing
`next_run_at`) lives in `app.worker.scheduler`; this module only computes
*what* a report should say for a given schedule and reference instant.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import RenderedMessage
from app.models.report import ReportSchedule
from app.models.team import Team
from app.models.template import MessageTemplate
from app.services import stats as stats_service
from app.services.templating import render_context

logger = logging.getLogger(__name__)

CADENCES = ("daily", "weekly", "monthly")

# Only a 'report'-kind template may be assigned to a ReportSchedule -- kept
# here (not in app.api.templates.SUPPORTED_KINDS) since it's this module's
# own concern which kinds it accepts for rendering; the API module imports
# this constant to keep its own SUPPORTED_KINDS in sync (see
# app/api/templates.py).
REPORT_TEMPLATE_KIND = "report"


class ReportData(TypedDict):
    team: str
    team_slug: str
    period_start: datetime
    period_end: datetime
    timezone: str
    summary: dict[str, Any]
    top_alerts: list[dict[str, Any]]
    volume: list[dict[str, Any]]
    breakdowns: dict[str, list[dict[str, Any]]]
    response_times: dict[str, Any]
    # Percent change in events_in_range vs. the immediately-preceding
    # same-length period, e.g. +12.5 or -8.0. None when the previous period
    # had zero events (a percentage relative to zero is undefined, not
    # "infinite" or "0%").
    delta_pct: float | None


# -- next-run scheduling -------------------------------------------------------


def compute_next_run(
    *, cadence: str, weekday: int | None, hour: int, timezone: str, after: datetime
) -> datetime:
    """The next occurrence strictly after `after` (a UTC-aware instant) that
    matches this schedule's cadence/weekday/hour in its own IANA `timezone`,
    returned as a UTC-aware datetime.

    DST-safe by construction: every candidate is built as a local wall-clock
    datetime via `zoneinfo` (`datetime(y, m, d, hour, tzinfo=ZoneInfo(tz))`),
    which resolves each candidate's own correct UTC offset for that specific
    calendar date -- never a fixed timedelta added in UTC, which would drift
    by an hour across a spring-forward/fall-back transition. A `weekly`
    schedule at 09:00 America/New_York, for example, keeps firing at 09:00
    local both before and after the March/November transitions, even though
    the UTC instant that corresponds to shifts by an hour.

    `cadence='daily'` fires every day at `hour`. `cadence='weekly'` fires on
    `weekday` (0=Monday .. 6=Sunday) at `hour`. `cadence='monthly'` always
    fires on the 1st of the month at `hour` -- there's no day-of-month
    selector in this phase's brief, so the 1st is the fixed convention.
    """
    tz = ZoneInfo(timezone)
    local_after = after.astimezone(tz)

    if cadence == "daily":
        candidate_date = local_after.date()
        candidate = datetime(candidate_date.year, candidate_date.month, candidate_date.day, hour, tzinfo=tz)
        if candidate <= local_after:
            candidate_date += timedelta(days=1)
            candidate = datetime(
                candidate_date.year, candidate_date.month, candidate_date.day, hour, tzinfo=tz
            )
        return candidate.astimezone(UTC)

    if cadence == "weekly":
        if weekday is None:
            raise ValueError("weekly cadence requires weekday")
        candidate_date = local_after.date()
        days_ahead = (weekday - candidate_date.weekday()) % 7
        candidate_date = candidate_date + timedelta(days=days_ahead)
        candidate = datetime(candidate_date.year, candidate_date.month, candidate_date.day, hour, tzinfo=tz)
        if candidate <= local_after:
            candidate_date += timedelta(days=7)
            candidate = datetime(
                candidate_date.year, candidate_date.month, candidate_date.day, hour, tzinfo=tz
            )
        return candidate.astimezone(UTC)

    if cadence == "monthly":
        year, month = local_after.year, local_after.month
        candidate = datetime(year, month, 1, hour, tzinfo=tz)
        if candidate <= local_after:
            month += 1
            if month > 12:
                month = 1
                year += 1
            candidate = datetime(year, month, 1, hour, tzinfo=tz)
        return candidate.astimezone(UTC)

    raise ValueError(f"unknown cadence {cadence!r}")


# -- period computation ---------------------------------------------------------


def compute_period(*, cadence: str, timezone: str, reference: datetime) -> tuple[datetime, datetime]:
    """The immediately-preceding full cadence period, in `timezone`'s own
    wall-clock, as of `reference` (a UTC-aware instant) -- e.g. `weekly`:
    last week's Monday 00:00 through this week's Monday 00:00 (both local
    to `timezone`), for whichever calendar week `reference` currently falls
    in. Returns a `(start, end)` pair of UTC-aware datetimes, `end`
    exclusive -- directly usable as `app.services.stats`'s own
    `from_ts`/`to_ts`.

    `reference` is normally either "now" (preview/run-now: report on
    whatever period just elapsed) or a schedule's own `next_run_at` value
    captured at claim time (the sweep: report on the period that ends
    exactly at the schedule's own due instant, so delivery jitter -- the
    sweep only runs once a minute -- never changes which period gets
    reported). See `app.worker.scheduler.dispatch_report_schedule`.

    Boundaries are computed on local calendar dates, via `zoneinfo` -- DST-
    safe for the same reason `compute_next_run` is: a local midnight is
    resolved to its own correct UTC offset for that specific date, not
    derived by subtracting a fixed 24h/7d/1-month timedelta from a UTC
    instant.

    Returned with `timezone`'s own `tzinfo` attached, NOT normalized to UTC:
    the two represent identical instants (so `app.services.stats`'s queries,
    and this module's own delta-vs-previous-period math, are unaffected
    either way), but `report_template_context`'s `datetime_format` filter
    formats a datetime using whatever tzinfo it's carrying -- a template
    author's `{{ period_start | datetime_format('%Y-%m-%d') }}` must show
    the schedule's OWN local calendar date, not the UTC date that same
    instant happens to fall on (which can be a full day off near local
    midnight, e.g. 2026-09-14 00:00 KST is still 2026-09-13 in UTC).
    """
    tz = ZoneInfo(timezone)
    local_ref = reference.astimezone(tz)

    if cadence == "daily":
        end_date = local_ref.date()
        start_date = end_date - timedelta(days=1)
    elif cadence == "weekly":
        end_date = local_ref.date() - timedelta(days=local_ref.weekday())
        start_date = end_date - timedelta(days=7)
    elif cadence == "monthly":
        end_date = local_ref.date().replace(day=1)
        prev_month_end = end_date - timedelta(days=1)
        start_date = prev_month_end.replace(day=1)
    else:
        raise ValueError(f"unknown cadence {cadence!r}")

    start = datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz)
    end = datetime(end_date.year, end_date.month, end_date.day, tzinfo=tz)
    return start, end


def _pct_delta(previous: int, current: int) -> float | None:
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


# -- report data ------------------------------------------------------------


async def build_report_data(
    session: AsyncSession, schedule: ReportSchedule, *, reference: datetime
) -> ReportData:
    """Build the full `ReportData` snapshot for `schedule`'s immediately-
    preceding cadence period (relative to `reference` -- see
    `compute_period`'s docstring for what that should be).

    Every stats figure is scoped to `schedule.team_id` alone (`cluster_ids`
    is always `None`) via `app.services.stats`'s functions, unchanged -- see
    this module's docstring for the AlertShare-exclusion caveat that
    inherits from there.
    """
    period_start, period_end = compute_period(
        cadence=schedule.cadence, timezone=schedule.timezone, reference=reference
    )
    period_length = period_end - period_start
    previous_start = period_start - period_length
    previous_end = period_start

    team = await session.get(Team, schedule.team_id)

    scope = {
        "team_id": schedule.team_id,
        "cluster_ids": None,
        "from_ts": period_start,
        "to_ts": period_end,
    }
    summary = await stats_service.summary(session, **scope)
    top_alerts = await stats_service.top_alerts(session, **scope, limit=10)
    volume = await stats_service.volume(session, **scope, bucket="day")
    by_severity = await stats_service.breakdown(session, **scope, by="severity")
    by_namespace = await stats_service.breakdown(session, **scope, by="namespace")
    response_times = await stats_service.response_times(session, **scope)

    previous_summary = await stats_service.summary(
        session,
        team_id=schedule.team_id,
        cluster_ids=None,
        from_ts=previous_start,
        to_ts=previous_end,
    )
    delta_pct = _pct_delta(previous_summary["events_in_range"], summary["events_in_range"])

    return ReportData(
        team=team.name if team is not None else str(schedule.team_id),
        team_slug=team.slug if team is not None else "",
        period_start=period_start,
        period_end=period_end,
        timezone=schedule.timezone,
        summary=summary,
        top_alerts=top_alerts,
        volume=volume,
        breakdowns={"severity": by_severity, "namespace": by_namespace},
        response_times=response_times,
        delta_pct=delta_pct,
    )


def report_template_context(data: ReportData) -> dict[str, Any]:
    """The variables a report template source can reference -- `data`'s own
    fields, plus `now()` (a zero-arg callable, same convention as
    `app.services.templating.notification_context`) so a template gets the
    actual render-time timestamp rather than whenever `data` was built.
    """
    return {**data, "now": lambda: datetime.now(UTC)}


# -- default template --------------------------------------------------------

DEFAULT_REPORT_TEMPLATES: dict[str, str] = {
    "title": (
        "[KAM] {{ team }} 주간 알럿 리포트 "
        "({{ period_start | datetime_format('%Y-%m-%d') }}~"
        "{{ period_end | datetime_format('%Y-%m-%d') }})"
    ),
    "body": (
        "{{ team }} 알럿 리포트\n"
        "기간: {{ period_start | datetime_format('%Y-%m-%d') }} ~ "
        "{{ period_end | datetime_format('%Y-%m-%d') }} ({{ timezone }})\n\n"
        "발생 건수: {{ summary.events_in_range }}"
        "{% if delta_pct is not none %} (전기간 대비 {{ delta_pct }}%){% endif %}\n"
        "발송 성공: {{ summary.delivered_in_range }} / 실패: {{ summary.failed_or_dead_in_range }}\n"
        "현재 firing 중: {{ summary.firing_now }}\n"
        "{% if response_times.mtta_seconds is not none %}"
        "평균 확인 시간(MTTA): {{ response_times.mtta_seconds | humanize_duration }}\n"
        "{% endif %}"
        "{% if response_times.mttr_seconds is not none %}"
        "평균 해결 시간(MTTR): {{ response_times.mttr_seconds | humanize_duration }}\n"
        "{% endif %}"
        "\nTop 알럿:\n"
        "{% for a in top_alerts %}  {{ loop.index }}. {{ a.alertname }} ({{ a.count }}건)\n{% endfor %}"
        "{% if not top_alerts %}  (해당 기간에 발생한 알럿이 없습니다)\n{% endif %}"
    ),
    "body_html": (
        "<h2>{{ team }} 알럿 리포트</h2>"
        "<p>기간: {{ period_start | datetime_format('%Y-%m-%d') }} ~ "
        "{{ period_end | datetime_format('%Y-%m-%d') }} ({{ timezone }})</p>"
        "<ul>"
        "<li>발생 건수: {{ summary.events_in_range }}"
        "{% if delta_pct is not none %} (전기간 대비 {{ delta_pct }}%){% endif %}</li>"
        "<li>발송 성공: {{ summary.delivered_in_range }} / 실패: {{ summary.failed_or_dead_in_range }}</li>"
        "<li>현재 firing 중: {{ summary.firing_now }}</li>"
        "</ul>"
        "<h3>Top 알럿</h3>"
        "<table border=\"1\" cellpadding=\"4\" cellspacing=\"0\">"
        "<tr><th>#</th><th>알럿명</th><th>건수</th></tr>"
        "{% for a in top_alerts %}"
        "<tr><td>{{ loop.index }}</td><td>{{ a.alertname }}</td><td>{{ a.count }}</td></tr>"
        "{% endfor %}"
        "</table>"
    ),
}


async def render_report(session: AsyncSession, schedule: ReportSchedule, data: ReportData) -> RenderedMessage:
    """Resolve `schedule.template_id` (only honored when its kind is still
    `'report'` -- a template's kind can be edited after the fact via
    `PUT /templates/{id}`, so this defensively re-checks rather than trusting
    whatever passed validation at the time the schedule was saved) or the
    built-in default report templates, and render.

    Never raises: a render failure (sandbox violation, syntax error, timeout,
    oversized output) falls back to rendering `DEFAULT_REPORT_TEMPLATES`
    instead, same "never let a broken template block delivery" posture as
    `app.services.templating.render()`.
    """
    template_strs: dict[str, str | None] = DEFAULT_REPORT_TEMPLATES
    if schedule.template_id is not None:
        template = await session.get(MessageTemplate, schedule.template_id)
        if template is not None and template.kind == REPORT_TEMPLATE_KIND:
            template_strs = {
                "title": template.title_template,
                "body": template.body_template,
                "body_html": template.body_html_template,
            }

    context = report_template_context(data)
    try:
        return await render_context(template_strs, context)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see docstring.
        logger.warning(
            "report render failed for schedule=%s (%s: %s) -- falling back to default template",
            schedule.id,
            type(exc).__name__,
            exc,
        )
        return await render_context(DEFAULT_REPORT_TEMPLATES, context)


# -- variable reference ---------------------------------------------------------

REPORT_TEMPLATE_VARIABLES: list[dict[str, str]] = [
    {"name": "team", "description": "팀 이름", "example": "{{ team }}"},
    {"name": "team_slug", "description": "팀 슬러그", "example": "{{ team_slug }}"},
    {
        "name": "period_start",
        "description": "리포트 기간 시작 시각 (datetime_format 필터 권장)",
        "example": "{{ period_start | datetime_format('%Y-%m-%d') }}",
    },
    {
        "name": "period_end",
        "description": "리포트 기간 종료 시각 (datetime_format 필터 권장)",
        "example": "{{ period_end | datetime_format('%Y-%m-%d') }}",
    },
    {"name": "timezone", "description": "이 스케줄의 기준 타임존", "example": "{{ timezone }}"},
    {
        "name": "delta_pct",
        "description": "직전 동기간 대비 발생 건수 증감률(%) -- 비교 대상이 0건이면 none",
        "example": "{{ delta_pct }}%",
    },
    {
        "name": "summary.events_in_range",
        "description": "기간 내 발생 건수",
        "example": "{{ summary.events_in_range }}",
    },
    {
        "name": "summary.firing_now",
        "description": "현재 시점 firing 중인 알럿 수 (기간과 무관한 실시간 값)",
        "example": "{{ summary.firing_now }}",
    },
    {
        "name": "summary.delivered_in_range",
        "description": "기간 내 발송 성공 건수",
        "example": "{{ summary.delivered_in_range }}",
    },
    {
        "name": "summary.failed_or_dead_in_range",
        "description": "기간 내 발송 실패/포기 건수",
        "example": "{{ summary.failed_or_dead_in_range }}",
    },
    {
        "name": "top_alerts",
        "description": "알럿명별 발생 건수 상위 10건 -- {alertname, count, receive_total} 리스트, for 루프로 순회",
        "example": "{% for a in top_alerts %}{{ a.alertname }}: {{ a.count }}{% endfor %}",
    },
    {
        "name": "volume",
        "description": "일별 발생 건수 -- {bucket_start, firing_count} 리스트",
        "example": "{% for v in volume %}{{ v.bucket_start | datetime_format('%Y-%m-%d') }}: {{ v.firing_count }}{% endfor %}",
    },
    {
        "name": "breakdowns.severity",
        "description": "심각도별 발생 건수 -- {key, count} 리스트",
        "example": "{% for b in breakdowns.severity %}{{ b.key }}: {{ b.count }}{% endfor %}",
    },
    {
        "name": "breakdowns.namespace",
        "description": "네임스페이스별 발생 건수 -- {key, count} 리스트",
        "example": "{% for b in breakdowns.namespace %}{{ b.key }}: {{ b.count }}{% endfor %}",
    },
    {
        "name": "response_times.mtta_seconds",
        "description": "평균 확인 소요 시간(초) -- 확인된 알럿이 없으면 none",
        "example": "{{ response_times.mtta_seconds | humanize_duration }}",
    },
    {
        "name": "response_times.mttr_seconds",
        "description": "평균 해결 소요 시간(초) -- 해결된 알럿이 없으면 none",
        "example": "{{ response_times.mttr_seconds | humanize_duration }}",
    },
    {
        "name": "now()",
        "description": "현재 시각(UTC)을 반환하는 호출 가능 함수",
        "example": "{{ now() | datetime_format }}",
    },
]


def report_template_variables() -> list[dict[str, str]]:
    return REPORT_TEMPLATE_VARIABLES
