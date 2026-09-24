"""Scheduled report CRUD + on-demand run-now/preview API (Phase 20).

Report *generation* (period math, stats aggregation, rendering) lives in
`app.services.reports`; periodic *delivery* (claiming due schedules, staging
outbox rows, advancing `next_run_at`) lives in `app.worker.scheduler`. This
module is only the HTTP surface: team-scoped CRUD (owner-gated for writes,
member-readable -- same RBAC split as `app/api/channels.py` and
`app/api/templates.py`), plus two on-demand actions a member/owner can
trigger outside the schedule's own cadence.
"""

from datetime import UTC, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, require_team_role
from app.db import get_session
from app.models.channel import Channel
from app.models.outbox import NotificationOutbox
from app.models.report import ReportSchedule
from app.models.team import Team, TeamMembership
from app.models.template import MessageTemplate
from app.models.user import User
from app.services import audit
from app.services import reports as reports_service
from app.services.reports import REPORT_TEMPLATE_KIND

router = APIRouter(prefix="/api/v1", tags=["reports"])

Cadence = Literal["daily", "weekly", "monthly"]


class ReportScheduleCreate(BaseModel):
    name: str
    enabled: bool = True
    cadence: Cadence = "weekly"
    weekday: int | None = None
    hour: int = Field(ge=0, le=23)
    timezone: str = "UTC"
    template_id: int | None = None
    channel_ids: list[int] = Field(min_length=1)


class ReportScheduleUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    cadence: Cadence | None = None
    # None is ambiguous between "not provided" and "explicitly clear it" --
    # update_report_schedule disambiguates via `model_fields_set`, same
    # convention as app.api.channels.ChannelUpdate.template_id.
    weekday: int | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = None
    template_id: int | None = None
    channel_ids: list[int] | None = Field(default=None, min_length=1)


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


async def _get_schedule_or_404(session: AsyncSession, schedule_id: int) -> ReportSchedule:
    """Always eager-loads `channels` -- every caller (including `_serialize`)
    accesses that relationship synchronously afterward, which would raise
    `MissingGreenlet` under SQLAlchemy's asyncio extension on a lazy-load
    outside an awaited context.
    """
    result = await session.execute(
        select(ReportSchedule)
        .where(ReportSchedule.id == schedule_id)
        .options(selectinload(ReportSchedule.channels))
    )
    schedule = result.scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="report schedule not found")
    return schedule


async def _require_team_role(session: AsyncSession, team_id: int, user: User, role: str) -> None:
    """Same RBAC rule as `deps.require_team_role`, callable after the fact
    once a schedule's team_id is known -- `/reports/{id}` routes are keyed
    by schedule id, not team_id, same reasoning as
    `app.api.channels`/`app.api.templates`'s own duplicated helper.
    """
    if user.is_admin:
        return
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    if role == "owner" and membership.role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


def _validate_cadence_fields(cadence: str, weekday: int | None, hour: int, timezone: str) -> None:
    if not 0 <= hour <= 23:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="hour must be 0-23")

    if cadence == "weekly":
        if weekday is None or not 0 <= weekday <= 6:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="weekly cadence requires weekday to be 0-6 (0=Monday)",
            )
    elif weekday is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"cadence '{cadence}' does not use weekday",
        )

    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"unknown timezone '{timezone}'"
        ) from exc


async def _resolve_channels(session: AsyncSession, team_id: int, channel_ids: list[int]) -> list[Channel]:
    result = await session.execute(
        select(Channel).where(Channel.id.in_(channel_ids), Channel.deleted_at.is_(None))
    )
    channels = result.scalars().all()
    found_ids = {c.id for c in channels}
    missing = sorted(set(channel_ids) - found_ids)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"unknown channel_ids: {missing}"
        )
    foreign = sorted(c.id for c in channels if c.team_id != team_id)
    if foreign:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"channel_ids must belong to this team: {foreign}",
        )
    return list(channels)


async def _validate_template(session: AsyncSession, team_id: int, template_id: int | None) -> None:
    if template_id is None:
        return
    template = await session.get(MessageTemplate, template_id)
    if template is None or template.team_id != team_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="template_id must belong to this team"
        )
    if template.kind != REPORT_TEMPLATE_KIND:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"template_id must be a '{REPORT_TEMPLATE_KIND}'-kind template",
        )


def _serialize(schedule: ReportSchedule) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "team_id": schedule.team_id,
        "name": schedule.name,
        "enabled": schedule.enabled,
        "cadence": schedule.cadence,
        "weekday": schedule.weekday,
        "hour": schedule.hour,
        "timezone": schedule.timezone,
        "template_id": schedule.template_id,
        "channel_ids": [c.id for c in schedule.channels],
        "next_run_at": schedule.next_run_at,
        "last_run_at": schedule.last_run_at,
        "last_status": schedule.last_status,
        "created_at": schedule.created_at,
    }


@router.get("/teams/{team_id}/reports")
async def list_report_schedules(
    team_id: int,
    session: AsyncSession = Depends(get_session),
    _member: User = Depends(require_team_role("member")),
) -> list[dict[str, Any]]:
    await _get_team_or_404(session, team_id)
    result = await session.execute(
        select(ReportSchedule)
        .where(ReportSchedule.team_id == team_id)
        .options(selectinload(ReportSchedule.channels))
        .order_by(ReportSchedule.name)
    )
    return [_serialize(s) for s in result.scalars().all()]


@router.post("/teams/{team_id}/reports", status_code=status.HTTP_201_CREATED)
async def create_report_schedule(
    team_id: int,
    body: ReportScheduleCreate,
    actor: User = Depends(require_team_role("owner")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    await _get_team_or_404(session, team_id)
    _validate_cadence_fields(body.cadence, body.weekday, body.hour, body.timezone)
    channels = await _resolve_channels(session, team_id, body.channel_ids)
    await _validate_template(session, team_id, body.template_id)

    now = datetime.now(UTC)
    schedule = ReportSchedule(
        team_id=team_id,
        name=body.name,
        enabled=body.enabled,
        cadence=body.cadence,
        weekday=body.weekday,
        hour=body.hour,
        timezone=body.timezone,
        template_id=body.template_id,
        channels=channels,
        next_run_at=reports_service.compute_next_run(
            cadence=body.cadence, weekday=body.weekday, hour=body.hour, timezone=body.timezone, after=now
        ),
    )
    session.add(schedule)
    await session.flush()

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team_id,
        action="report.create",
        object_type="report_schedule",
        object_ref=schedule.name,
    )
    await session.commit()
    return _serialize(schedule)


@router.get("/reports/{report_id}")
async def get_report_schedule(
    report_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    schedule = await _get_schedule_or_404(session, report_id)
    await _require_team_role(session, schedule.team_id, actor, "member")
    return _serialize(schedule)


@router.patch("/reports/{report_id}")
async def update_report_schedule(
    report_id: int,
    body: ReportScheduleUpdate,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    schedule = await _get_schedule_or_404(session, report_id)
    await _require_team_role(session, schedule.team_id, actor, "owner")

    if body.name is not None:
        schedule.name = body.name
    if body.enabled is not None:
        schedule.enabled = body.enabled
    if body.cadence is not None:
        schedule.cadence = body.cadence
    if "weekday" in body.model_fields_set:
        schedule.weekday = body.weekday
    if body.hour is not None:
        schedule.hour = body.hour
    if body.timezone is not None:
        schedule.timezone = body.timezone

    # Validated against the FINAL merged state, not just this one PATCH body
    # in isolation -- same posture as app.api.channels.update_channel's own
    # digest_mode/rate_limit_per_hour cross-field re-check.
    _validate_cadence_fields(schedule.cadence, schedule.weekday, schedule.hour, schedule.timezone)

    if "template_id" in body.model_fields_set:
        await _validate_template(session, schedule.team_id, body.template_id)
        schedule.template_id = body.template_id

    if body.channel_ids is not None:
        schedule.channels = await _resolve_channels(session, schedule.team_id, body.channel_ids)

    # Recomputed on every update, not just a cadence-field change -- cheap,
    # and avoids a stale next_run_at surviving an edit (e.g. changing hour
    # from 9 to 14 should move today's still-pending run, not wait for the
    # next cycle before the new hour takes effect).
    schedule.next_run_at = reports_service.compute_next_run(
        cadence=schedule.cadence,
        weekday=schedule.weekday,
        hour=schedule.hour,
        timezone=schedule.timezone,
        after=datetime.now(UTC),
    )

    await audit.log(
        session,
        user_id=actor.id,
        team_id=schedule.team_id,
        action="report.update",
        object_type="report_schedule",
        object_ref=schedule.name,
    )
    await session.commit()
    return _serialize(schedule)


@router.delete("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report_schedule(
    report_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    schedule = await _get_schedule_or_404(session, report_id)
    await _require_team_role(session, schedule.team_id, actor, "owner")

    await audit.log(
        session,
        user_id=actor.id,
        team_id=schedule.team_id,
        action="report.delete",
        object_type="report_schedule",
        object_ref=schedule.name,
    )
    await session.delete(schedule)
    await session.commit()


@router.post("/reports/{report_id}/run-now")
async def run_report_now(
    report_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Build + render + stage outbox rows immediately, for whatever period
    `compute_period` resolves as of right now -- does NOT advance
    `next_run_at` (the schedule's own cadence timer is untouched; this is a
    manual, out-of-band trigger, not an early firing of the real one).
    `last_run_at`/`last_status` ARE updated either way, same as a real sweep
    dispatch.
    """
    schedule = await _get_schedule_or_404(session, report_id)
    await _require_team_role(session, schedule.team_id, actor, "owner")

    now = datetime.now(UTC)
    queued_channels = 0
    error: str | None = None
    try:
        data = await reports_service.build_report_data(session, schedule, reference=now)
        message = await reports_service.render_report(session, schedule, data)
        for channel in schedule.channels:
            if channel.deleted_at is not None:
                continue
            session.add(
                NotificationOutbox(
                    alert_event_id=None,
                    routing_rule_id=None,
                    channel_id=channel.id,
                    team_id=schedule.team_id,
                    trigger="report",
                    payload={
                        "rendered": message.model_dump(mode="json"),
                        "schedule_id": schedule.id,
                        "period": {
                            "start": data["period_start"].isoformat(),
                            "end": data["period_end"].isoformat(),
                        },
                    },
                    is_digest=False,
                    status="pending",
                )
            )
            queued_channels += 1
    except Exception as exc:  # noqa: BLE001 -- surfaced to the caller below.
        error = f"{type(exc).__name__}: {exc}"

    schedule.last_run_at = now
    schedule.last_status = "ok" if error is None else f"error: {error}"[:500]
    await audit.log(
        session,
        user_id=actor.id,
        team_id=schedule.team_id,
        action="report.run_now",
        object_type="report_schedule",
        object_ref=schedule.name,
        detail={"queued_channels": queued_channels, "error": error},
    )
    await session.commit()

    if error is not None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"report generation failed: {error}"
        )
    return {"queued_channels": queued_channels}


@router.get("/reports/{report_id}/preview")
async def preview_report(
    report_id: int,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Build + render against the current period, with no delivery at all --
    the schedule's `next_run_at`/`last_run_at`/`last_status` are all left
    untouched.
    """
    schedule = await _get_schedule_or_404(session, report_id)
    await _require_team_role(session, schedule.team_id, actor, "member")

    data = await reports_service.build_report_data(session, schedule, reference=datetime.now(UTC))
    message = await reports_service.render_report(session, schedule, data)
    return {"title": message.title, "body": message.body, "body_html": message.body_html}
