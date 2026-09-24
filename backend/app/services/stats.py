"""Statistics aggregation over alert_events (+ notification_outbox for
delivery counts) -- backs the Stats dashboard (Phase 19) and is reused
as-is by the Phase 20 weekly report generator, so every function's
signature here is load-bearing: `(session, *, team_id, cluster_ids,
from_ts, to_ts, ...)`.

Every function excludes `is_test` rows (synthetic POST .../test-alert
events would otherwise pollute real incident statistics) and applies the
same team/cluster scoping: `team_id=None` means "every team" (the admin
unscoped view), a concrete id means "only that team's events". Callers
resolve+authorize `team_id` themselves via `app.api.deps.resolve_team_scope`
before calling in here -- this module trusts whatever it's given.

Deliberately narrower than /alerts' own team scoping: Phase 14's AlertShare
widening (a team's /live and /history additionally show a *view*/
*view_notify*-shared owner team's matching alerts -- see
app.api.alerts._load_shared_scopes_by_slug/`shared_owner_team_ids`) is NOT
applied here. `AlertEvent.team_id == team_id` alone decides membership in
every aggregate below, full stop -- a team's stats describe its own team's
incidents only, never another team's shared-in ones, even if that same team
would see those alerts on /live. Phase 20's weekly report inherits this
same "own incidents only" semantics by construction, since it calls these
functions unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import UTCDateTime
from app.models.alert import AlertEvent
from app.models.outbox import NotificationOutbox
from app.models.team import Team

BucketKind = Literal["hour", "day"]
BreakdownDimension = Literal["namespace", "severity", "team", "cluster"]

_NONE_KEY = "none"


def _base_conditions(
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
) -> list[ColumnElement[bool]]:
    """Shared by every function below. Filters on `starts_at` (an alert
    episode's own occurrence time), not `last_received_at` -- unlike
    /alerts/history's filter, stats are about when incidents *happened*,
    not when they were last re-delivered, and `volume`'s bucketing below
    must use the same column its own WHERE clause filters on or a bucket at
    the range's edge could include events the WHERE excluded (or vice
    versa).
    """
    conditions: list[ColumnElement[bool]] = [
        AlertEvent.is_test.is_(False),
        AlertEvent.starts_at >= from_ts,
        AlertEvent.starts_at < to_ts,
    ]
    if team_id is not None:
        conditions.append(AlertEvent.team_id == team_id)
    if cluster_ids:
        conditions.append(AlertEvent.cluster_id.in_(cluster_ids))
    return conditions


async def top_alerts(
    session: AsyncSession,
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
    limit: int = 10,
) -> list[dict[str, Any]]:
    conditions = _base_conditions(
        team_id=team_id, cluster_ids=cluster_ids, from_ts=from_ts, to_ts=to_ts
    )
    result = await session.execute(
        select(
            AlertEvent.alertname,
            func.count().label("count"),
            func.sum(AlertEvent.receive_count).label("receive_total"),
        )
        .where(*conditions)
        .group_by(AlertEvent.alertname)
        .order_by(func.count().desc())
        .limit(limit)
    )
    return [
        {"alertname": alertname, "count": count, "receive_total": receive_total or 0}
        for alertname, count, receive_total in result.all()
    ]


def _bucket_column(bucket: BucketKind, dialect_name: str):
    """Dialect-branched: Postgres has `date_trunc`, SQLite doesn't -- it
    falls back to `strftime` with a format that zeroes out everything finer
    than the requested bucket (so e.g. every timestamp within the same hour
    formats identically and therefore groups together).

    Both branches are hinted with `type_=UTCDateTime` so the result column
    round-trips as a tz-aware UTC `datetime` regardless of dialect, the same
    invariant every other `starts_at`-derived value in this app holds --
    callers (and Phase 20's report) get one consistent type back rather than
    a driver-dependent str/datetime split.
    """
    if dialect_name == "postgresql":
        unit = "hour" if bucket == "hour" else "day"
        return func.date_trunc(unit, AlertEvent.starts_at, type_=UTCDateTime())

    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    return func.strftime(fmt, AlertEvent.starts_at, type_=UTCDateTime())


async def volume(
    session: AsyncSession,
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
    bucket: BucketKind = "day",
) -> list[dict[str, Any]]:
    conditions = _base_conditions(
        team_id=team_id, cluster_ids=cluster_ids, from_ts=from_ts, to_ts=to_ts
    )
    dialect_name = session.get_bind().dialect.name
    bucket_col = _bucket_column(bucket, dialect_name).label("bucket_start")

    result = await session.execute(
        select(bucket_col, func.count().label("firing_count"))
        .where(*conditions)
        .group_by(bucket_col)
        .order_by(bucket_col)
    )
    return [
        {"bucket_start": bucket_start, "firing_count": firing_count}
        for bucket_start, firing_count in result.all()
    ]


async def breakdown(
    session: AsyncSession,
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
    by: BreakdownDimension,
) -> list[dict[str, Any]]:
    """Grouped counts along one dimension. A NULL group value (no severity
    label, no namespace label, no `kam_team` match) normalizes to the
    literal string "none" -- the same placeholder /alerts/live and
    /alerts/history already use for a missing severity, applied uniformly
    across all four dimensions here for one consistent frontend contract.

    `by="team"` is admin-oriented: combined with a `team_id` filter (a
    non-admin's forced own-team scope) every row collapses to that one team
    by construction, since the WHERE clause already restricts to it -- no
    special-casing needed here, the caller's own scoping does the work.
    """
    conditions = _base_conditions(
        team_id=team_id, cluster_ids=cluster_ids, from_ts=from_ts, to_ts=to_ts
    )

    if by == "cluster":
        key_col = AlertEvent.cluster_name
    elif by == "severity":
        key_col = AlertEvent.severity
    elif by == "namespace":
        key_col = AlertEvent.namespace
    else:
        key_col = Team.slug

    stmt = select(key_col.label("key"), func.count().label("count")).select_from(AlertEvent)
    if by == "team":
        # LEFT JOIN: an event with no team_id (never matched a `kam_team`
        # label) must still be counted, under the "none" placeholder, not
        # silently dropped by an inner join.
        stmt = stmt.outerjoin(Team, Team.id == AlertEvent.team_id)
    stmt = stmt.where(*conditions).group_by(key_col).order_by(func.count().desc())

    result = await session.execute(stmt)
    return [{"key": key if key is not None else _NONE_KEY, "count": count} for key, count in result.all()]


async def response_times(
    session: AsyncSession,
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
) -> dict[str, Any]:
    """MTTA (first_received_at -> acknowledged_at, acked events only) and
    MTTR (starts_at -> ends_at, resolved events only), averaged in Python
    rather than via a SQL AVG(epoch diff): Postgres and SQLite have no
    portable common expression for "seconds between two timestamps" (
    EXTRACT(EPOCH FROM ...) vs. julianday() arithmetic), and the row counts
    involved (events acked/resolved within one filtered range) are small
    enough that fetching just the two timestamp columns and averaging here
    is simpler than a second dialect branch.

    0 matching rows -> None (not 0) for the corresponding *_seconds field,
    same as SQL's own AVG() over no rows -- an empty MTTA average must not
    read as "everything was acknowledged instantly".
    """
    conditions = _base_conditions(
        team_id=team_id, cluster_ids=cluster_ids, from_ts=from_ts, to_ts=to_ts
    )

    acked_result = await session.execute(
        select(AlertEvent.first_received_at, AlertEvent.acknowledged_at).where(
            *conditions, AlertEvent.acknowledged_at.is_not(None)
        )
    )
    acked_rows = acked_result.all()
    acked_count = len(acked_rows)
    mtta_seconds = (
        sum((acked_at - first).total_seconds() for first, acked_at in acked_rows) / acked_count
        if acked_count
        else None
    )

    resolved_result = await session.execute(
        select(AlertEvent.starts_at, AlertEvent.ends_at).where(
            *conditions, AlertEvent.ends_at.is_not(None)
        )
    )
    resolved_rows = resolved_result.all()
    resolved_count = len(resolved_rows)
    mttr_seconds = (
        sum((ends - starts).total_seconds() for starts, ends in resolved_rows) / resolved_count
        if resolved_count
        else None
    )

    return {
        "mtta_seconds": mtta_seconds,
        "mttr_seconds": mttr_seconds,
        "acked_count": acked_count,
        "resolved_count": resolved_count,
    }


def _outbox_scope_conditions(
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
) -> tuple[list[ColumnElement[bool]], bool]:
    """Shared WHERE-clause fragment for the two outbox counts in `summary`.

    `NotificationOutbox` has no `cluster_id` of its own, so a cluster filter
    needs an INNER join to `AlertEvent` -- which also, as a side effect,
    excludes every digest AGGREGATE row (Phase 16: `alert_event_id IS NULL`,
    since one aggregate send can bundle events across many
    clusters/is_test values and so can't be attributed to a single one
    either way). Without a cluster filter, an OUTER join keeps aggregate
    rows in (via the `AlertEvent.id IS NULL` half of the is_test condition)
    while still excluding a real test-alert's own outbox rows.

    Returns (conditions, joined_inner) -- the caller still needs to know
    which join to use.
    """
    conditions: list[ColumnElement[bool]] = [
        NotificationOutbox.created_at >= from_ts,
        NotificationOutbox.created_at < to_ts,
    ]
    if team_id is not None:
        conditions.append(NotificationOutbox.team_id == team_id)

    if cluster_ids:
        conditions.append(AlertEvent.cluster_id.in_(cluster_ids))
        conditions.append(AlertEvent.is_test.is_(False))
        return conditions, True

    conditions.append(or_(AlertEvent.id.is_(None), AlertEvent.is_test.is_(False)))
    return conditions, False


async def _count_outbox(
    session: AsyncSession,
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
    status_condition: ColumnElement[bool],
) -> int:
    conditions, joined_inner = _outbox_scope_conditions(
        team_id=team_id, cluster_ids=cluster_ids, from_ts=from_ts, to_ts=to_ts
    )
    conditions.append(status_condition)

    stmt = select(func.count()).select_from(NotificationOutbox)
    if joined_inner:
        stmt = stmt.join(AlertEvent, AlertEvent.id == NotificationOutbox.alert_event_id)
    else:
        stmt = stmt.outerjoin(AlertEvent, AlertEvent.id == NotificationOutbox.alert_event_id)
    stmt = stmt.where(*conditions)

    return (await session.execute(stmt)).scalar_one()


async def summary(
    session: AsyncSession,
    *,
    team_id: int | None,
    cluster_ids: list[int] | None,
    from_ts: datetime,
    to_ts: datetime,
) -> dict[str, Any]:
    """`firing_now` is a live snapshot ("how many alerts are open right
    now"), deliberately independent of `[from_ts, to_ts]` -- unlike the
    other three fields, which all describe what happened *within* the
    requested range.

    `failed_or_dead_in_range` counts a 'dead' row (permanently given up,
    per app.worker.outbox.MAX_ATTEMPTS) together with a 'pending' row that
    has already failed at least once and is awaiting retry
    (`attempts > 0`) -- both read as "currently in trouble" to a viewer of
    this card. A row that failed once but has since delivered
    successfully is not counted (its final status is 'delivered'), and
    'in_progress'/'digested' rows are excluded as neither failed nor dead.
    """
    firing_conditions: list[ColumnElement[bool]] = [
        AlertEvent.is_test.is_(False),
        AlertEvent.status == "firing",
    ]
    if team_id is not None:
        firing_conditions.append(AlertEvent.team_id == team_id)
    if cluster_ids:
        firing_conditions.append(AlertEvent.cluster_id.in_(cluster_ids))
    firing_now = (
        await session.execute(
            select(func.count()).select_from(AlertEvent).where(*firing_conditions)
        )
    ).scalar_one()

    range_conditions = _base_conditions(
        team_id=team_id, cluster_ids=cluster_ids, from_ts=from_ts, to_ts=to_ts
    )
    events_in_range = (
        await session.execute(
            select(func.count()).select_from(AlertEvent).where(*range_conditions)
        )
    ).scalar_one()

    delivered_in_range = await _count_outbox(
        session,
        team_id=team_id,
        cluster_ids=cluster_ids,
        from_ts=from_ts,
        to_ts=to_ts,
        status_condition=NotificationOutbox.status == "delivered",
    )
    failed_or_dead_in_range = await _count_outbox(
        session,
        team_id=team_id,
        cluster_ids=cluster_ids,
        from_ts=from_ts,
        to_ts=to_ts,
        status_condition=or_(
            NotificationOutbox.status == "dead",
            and_(NotificationOutbox.status == "pending", NotificationOutbox.attempts > 0),
        ),
    )

    return {
        "firing_now": firing_now,
        "events_in_range": events_in_range,
        "delivered_in_range": delivered_in_range,
        "failed_or_dead_in_range": failed_or_dead_in_range,
    }
