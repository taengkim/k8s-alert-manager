"""Statistics dashboard API: team-scoped aggregates over alert_events (+
notification_outbox), backing the Stats page. Every route shares
app.api.deps.resolve_team_scope's semantics with /alerts -- non-admins must
supply a `team_id` they belong to, admins may omit it for the unscoped
all-teams view.

Note this is *authorization* scoping only, not *visibility* scoping: unlike
/alerts' own /live and /history, a team's stats here never widen to include
another team's alerts shared into it via Phase 14's AlertShare (view/
view_notify) -- see app.services.stats's module docstring. A team member
authorized to view shared-in alerts on /live still only ever sees their own
team's incidents reflected in these aggregates.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, resolve_team_scope
from app.db import get_session
from app.models.user import User
from app.services import stats as stats_service

router = APIRouter(prefix="/api/v1/stats", tags=["stats"])

DEFAULT_RANGE_DAYS = 7
MAX_RANGE_DAYS = 90


def _as_utc(value: datetime) -> datetime:
    # Query-param datetimes with no offset are treated as already-UTC, the
    # same convention app.db.UTCDateTime applies to a naive value on write.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _resolve_range(from_ts: datetime | None, to_ts: datetime | None) -> tuple[datetime, datetime]:
    """Defaults to the trailing 7 days when both are omitted. Caps the span
    at MAX_RANGE_DAYS (422) -- a wide-open unbounded scan would otherwise let
    one request aggregate the entire alert_events table. A zero-width or
    even inverted range is deliberately NOT rejected here: it's a valid
    "empty period" query (every function above just returns empty/None
    results for it), not a client error.
    """
    resolved_to = _as_utc(to_ts) if to_ts is not None else datetime.now(UTC)
    resolved_from = (
        _as_utc(from_ts) if from_ts is not None else resolved_to - timedelta(days=DEFAULT_RANGE_DAYS)
    )

    if resolved_to - resolved_from > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"기간은 최대 {MAX_RANGE_DAYS}일까지 조회할 수 있습니다",
        )
    return resolved_from, resolved_to


async def _resolve_scope(
    *,
    team_id: int | None,
    cluster_id: list[int],
    from_ts: datetime | None,
    to_ts: datetime | None,
    user: User,
    session: AsyncSession,
) -> dict[str, Any]:
    team = await resolve_team_scope(team_id, user, session)
    resolved_from, resolved_to = _resolve_range(from_ts, to_ts)
    return {
        "team_id": team.id if team is not None else None,
        "cluster_ids": cluster_id or None,
        "from_ts": resolved_from,
        "to_ts": resolved_to,
    }


@router.get("/top-alerts")
async def get_top_alerts(
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    scope = await _resolve_scope(
        team_id=team_id, cluster_id=cluster_id, from_ts=from_ts, to_ts=to_ts, user=user, session=session
    )
    return await stats_service.top_alerts(session, **scope, limit=limit)


@router.get("/volume")
async def get_volume(
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    bucket: Literal["hour", "day"] = Query(default="day"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    scope = await _resolve_scope(
        team_id=team_id, cluster_id=cluster_id, from_ts=from_ts, to_ts=to_ts, user=user, session=session
    )
    return await stats_service.volume(session, **scope, bucket=bucket)


@router.get("/breakdown")
async def get_breakdown(
    by: Literal["namespace", "severity", "team", "cluster"] = Query(...),
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    scope = await _resolve_scope(
        team_id=team_id, cluster_id=cluster_id, from_ts=from_ts, to_ts=to_ts, user=user, session=session
    )
    return await stats_service.breakdown(session, **scope, by=by)


@router.get("/response-times")
async def get_response_times(
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    scope = await _resolve_scope(
        team_id=team_id, cluster_id=cluster_id, from_ts=from_ts, to_ts=to_ts, user=user, session=session
    )
    return await stats_service.response_times(session, **scope)


@router.get("/summary")
async def get_summary(
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    scope = await _resolve_scope(
        team_id=team_id, cluster_id=cluster_id, from_ts=from_ts, to_ts=to_ts, user=user, session=session
    )
    return await stats_service.summary(session, **scope)
