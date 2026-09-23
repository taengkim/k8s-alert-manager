"""Live alert fan-out endpoint: proxies each enabled cluster's Alertmanager,
scoped by team and filtered server-side.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, require_team_role
from app.db import get_session
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.comment import MAX_COMMENT_LENGTH, AlertComment
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.team import Team, TeamMembership
from app.models.user import User
from app.services import audit
from app.services.alertmanager import AlertmanagerClient, AlertmanagerUnavailableError
from app.services.grafana import resolve_grafana_url
from app.services.ingest import (
    AlertmanagerAlert,
    AlertmanagerWebhookPayload,
    ingest_webhook,
)
from app.services.routing import evaluate, route_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])


def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http_client


async def _resolve_team_scope(
    team_id: int | None, user: User, session: AsyncSession
) -> Team | None:
    """Authorize the requested team scope and return it (or None for admin's
    unscoped "all alerts" view).

    Non-admins must supply a `team_id` they belong to. Admins may omit it to
    see every alert, including ones without a `kam_team` label.
    """
    if team_id is None:
        if not user.is_admin:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="team_id is required",
            )
        return None

    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")

    if not user.is_admin:
        result = await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user.id
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    return team


def _flatten(cluster: Cluster, raw: dict[str, Any]) -> dict[str, Any]:
    labels = raw.get("labels") or {}
    annotations = raw.get("annotations") or {}
    status_obj = raw.get("status") or {}
    alertname = labels.get("alertname", "")
    return {
        "fingerprint": raw.get("fingerprint"),
        "alertname": alertname,
        "severity": labels.get("severity", ""),
        "namespace": labels.get("namespace", ""),
        "cluster": cluster.name,
        "state": status_obj.get("state", ""),
        "labels": labels,
        "annotations": annotations,
        "starts_at": raw.get("startsAt"),
        "generator_url": raw.get("generatorURL"),
        "grafana_url": resolve_grafana_url(annotations, cluster, alertname),
        "silenced_by": status_obj.get("silencedBy") or [],
    }


async def _fetch_cluster_alerts(
    cluster: Cluster, http_client: httpx.AsyncClient
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Fetch+flatten one cluster's alerts. Any failure -- connect/timeout,
    a malformed (non-JSON or non-list) response body, whatever -- degrades
    to an errors[] entry for this cluster rather than failing the whole
    fan-out (asyncio.gather has no return_exceptions, so an uncaught
    exception here would 500 the entire request and blank out every other
    cluster's alerts too).
    """
    client = AlertmanagerClient(cluster, http_client)
    try:
        raw_alerts = await client.get_alerts()
        return cluster.name, [_flatten(cluster, a) for a in raw_alerts], None
    except AlertmanagerUnavailableError as exc:
        return cluster.name, [], str(exc)
    except Exception:
        logger.exception(
            "unexpected error fetching alerts for cluster '%s'", cluster.name
        )
        return cluster.name, [], "unexpected error fetching alerts"


@router.get("/live")
async def get_live_alerts(
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    severity: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    search: str | None = Query(default=None),
    state: Literal["active", "suppressed"] | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> dict[str, Any]:
    team = await _resolve_team_scope(team_id, user, session)

    cluster_conditions = [Cluster.enabled.is_(True)]
    if cluster_id:
        # The ClusterFilter header select narrows the fan-out to just these
        # clusters -- still intersected with enabled, same as the unfiltered
        # default, rather than letting a stale/disabled cluster id sneak
        # back into the fan-out.
        cluster_conditions.append(Cluster.id.in_(cluster_id))

    clusters = (
        (await session.execute(select(Cluster).where(*cluster_conditions))).scalars().all()
    )

    fetch_results = await asyncio.gather(
        *(_fetch_cluster_alerts(cluster, http_client) for cluster in clusters)
    )

    alerts: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for cluster_name, cluster_alerts, error in fetch_results:
        if error is not None:
            errors.append({"cluster": cluster_name, "message": error})
            continue
        alerts.extend(cluster_alerts)

    if team is not None:
        alerts = [a for a in alerts if a["labels"].get("kam_team") == team.slug]

    if severity:
        # "none" is a synthetic value the frontend offers for alerts with no
        # severity label at all (labels.get("severity", "") flattens to "").
        wanted = {s.strip() for s in severity.split(",") if s.strip()}
        alerts = [a for a in alerts if (a["severity"] or "none") in wanted]

    if namespace:
        alerts = [a for a in alerts if a["namespace"] == namespace]

    if state:
        alerts = [a for a in alerts if a["state"] == state]

    if search:
        needle = search.lower()
        alerts = [a for a in alerts if needle in a["alertname"].lower()]

    return {"alerts": alerts, "errors": errors}


async def _resolve_usernames(session: AsyncSession, user_ids: set[int | None]) -> dict[int, str]:
    """Batch-resolve user ids (e.g. every acknowledged_by/assignee_user_id
    across a history page) to usernames in one query, rather than an N+1
    lookup per event.
    """
    ids = {uid for uid in user_ids if uid is not None}
    if not ids:
        return {}
    result = await session.execute(select(User.id, User.username).where(User.id.in_(ids)))
    return dict(result.all())


def _user_ref(user_id: int | None, usernames: dict[int, str]) -> dict[str, Any] | None:
    if user_id is None:
        return None
    return {"id": user_id, "username": usernames.get(user_id)}


def _serialize_event_summary(event: AlertEvent, usernames: dict[int, str]) -> dict[str, Any]:
    return {
        "id": event.id,
        "cluster_id": event.cluster_id,
        "cluster_name": event.cluster_name,
        "fingerprint": event.fingerprint,
        "status": event.status,
        "alertname": event.alertname,
        "severity": event.severity,
        "namespace": event.namespace,
        "team_id": event.team_id,
        "starts_at": event.starts_at,
        "ends_at": event.ends_at,
        "first_received_at": event.first_received_at,
        "last_received_at": event.last_received_at,
        "receive_count": event.receive_count,
        "is_test": event.is_test,
        "acknowledged_at": event.acknowledged_at,
        "acknowledged_by": _user_ref(event.acknowledged_by, usernames),
        "assignee": _user_ref(event.assignee_user_id, usernames),
    }


def _serialize_event_detail(
    event: AlertEvent, usernames: dict[int, str], grafana_url: str | None
) -> dict[str, Any]:
    return {
        **_serialize_event_summary(event, usernames),
        "labels": event.labels,
        "annotations": event.annotations,
        "generator_url": event.generator_url,
        "grafana_url": grafana_url,
    }


async def _serialize_one_detail(session: AsyncSession, event: AlertEvent) -> dict[str, Any]:
    """Convenience for endpoints returning exactly one event (ack/assignee/
    resolve-test/detail) -- resolves just that event's own ack/assignee
    usernames rather than pulling in `_resolve_usernames`' page-batching for
    a single row, plus its Grafana deep link (annotation, falling back to
    the owning cluster's `grafana_url` -- see `resolve_grafana_url`).
    """
    usernames = await _resolve_usernames(session, {event.acknowledged_by, event.assignee_user_id})
    cluster = await session.get(Cluster, event.cluster_id)
    grafana_url = resolve_grafana_url(event.annotations, cluster, event.alertname)
    return _serialize_event_detail(event, usernames, grafana_url)


def _build_history_conditions(
    *,
    team: Team | None,
    cluster_id: list[int],
    status_filter: str | None,
    severity: str | None,
    namespace: str | None,
    search: str | None,
    from_ts: datetime | None,
    to_ts: datetime | None,
    include_test: bool,
) -> list[ColumnElement[bool]]:
    """Shared by GET /history (paginated) and GET /history/export (the whole
    matching set) -- every filter must behave identically between the two."""
    conditions: list[ColumnElement[bool]] = []
    if not include_test:
        conditions.append(AlertEvent.is_test.is_(False))
    if team is not None:
        conditions.append(AlertEvent.team_id == team.id)
    if cluster_id:
        conditions.append(AlertEvent.cluster_id.in_(cluster_id))
    if status_filter:
        conditions.append(AlertEvent.status == status_filter)
    if severity:
        # "none" is a synthetic value (mirrors /live) for events with no
        # severity label at all (severity is NULL, not "").
        wanted = {s.strip().lower() for s in severity.split(",") if s.strip()}
        severity_conditions = []
        if "none" in wanted:
            wanted.discard("none")
            severity_conditions.append(AlertEvent.severity.is_(None))
        if wanted:
            severity_conditions.append(AlertEvent.severity.in_(wanted))
        if severity_conditions:
            conditions.append(or_(*severity_conditions))
    if namespace:
        conditions.append(AlertEvent.namespace == namespace)
    if search:
        # Escape the user's literal `%`/`_` so they filter as literal
        # characters instead of SQL LIKE wildcards.
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(AlertEvent.alertname.ilike(f"%{escaped}%", escape="\\"))
    if from_ts:
        conditions.append(AlertEvent.last_received_at >= from_ts)
    if to_ts:
        conditions.append(AlertEvent.last_received_at <= to_ts)
    return conditions


@router.get("/history")
async def get_alert_history(
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    status_filter: Literal["firing", "resolved"] | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    search: str | None = Query(default=None),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    include_test: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Server-paginated alert_events history, scoped by team using the same
    semantics as /live: non-admins must supply a team_id they belong to;
    admins may omit it to see everything, including unassigned events.

    `include_test` defaults to excluding synthetic POST .../test-alert rows
    from the default view -- they'd otherwise clutter real incident history.
    """
    team = await _resolve_team_scope(team_id, user, session)
    conditions = _build_history_conditions(
        team=team,
        cluster_id=cluster_id,
        status_filter=status_filter,
        severity=severity,
        namespace=namespace,
        search=search,
        from_ts=from_ts,
        to_ts=to_ts,
        include_test=include_test,
    )

    total = (
        await session.execute(
            select(func.count()).select_from(AlertEvent).where(*conditions)
        )
    ).scalar_one()

    result = await session.execute(
        select(AlertEvent)
        .where(*conditions)
        # id.desc() breaks ties within the same last_received_at (a batch
        # of events all stamped with one `now()`) so pagination has a
        # stable order instead of depending on incidental storage order.
        .order_by(AlertEvent.last_received_at.desc(), AlertEvent.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = result.scalars().all()

    usernames = await _resolve_usernames(
        session, {e.acknowledged_by for e in items} | {e.assignee_user_id for e in items}
    )

    return {
        "items": [_serialize_event_summary(e, usernames) for e in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# -- history export ---------------------------------------------------------
#
# json is capped small enough (10k rows) to build as one in-memory document;
# ndjson goes up to 100k and is genuinely streamed, one HISTORY_EXPORT_PAGE_SIZE
# page at a time, so a large export never holds its full result set in memory.
# Both constants are read directly (not captured into a default argument) so
# tests can `monkeypatch.setattr` them to exercise the cap logic without
# actually creating tens of thousands of rows.

HISTORY_EXPORT_JSON_CAP = 10_000
HISTORY_EXPORT_NDJSON_CAP = 100_000
HISTORY_EXPORT_PAGE_SIZE = 1_000


def _export_row(event: AlertEvent) -> dict[str, Any]:
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": event.id,
        "cluster_id": event.cluster_id,
        "cluster_name": event.cluster_name,
        "fingerprint": event.fingerprint,
        "status": event.status,
        "alertname": event.alertname,
        "severity": event.severity,
        "namespace": event.namespace,
        "team_id": event.team_id,
        "labels": event.labels,
        "annotations": event.annotations,
        "starts_at": _iso(event.starts_at),
        "ends_at": _iso(event.ends_at),
        "first_received_at": _iso(event.first_received_at),
        "last_received_at": _iso(event.last_received_at),
        "receive_count": event.receive_count,
        "is_test": event.is_test,
        "generator_url": event.generator_url,
        "acknowledged_at": _iso(event.acknowledged_at),
        "acknowledged_by": event.acknowledged_by,
        "assignee_user_id": event.assignee_user_id,
    }


async def _stream_history_ndjson(
    session: AsyncSession, conditions: list[ColumnElement[bool]]
) -> AsyncIterator[bytes]:
    offset = 0
    while True:
        result = await session.execute(
            select(AlertEvent)
            .where(*conditions)
            .order_by(AlertEvent.last_received_at.desc(), AlertEvent.id.desc())
            .offset(offset)
            .limit(HISTORY_EXPORT_PAGE_SIZE)
        )
        rows = result.scalars().all()
        for row in rows:
            yield (json.dumps(_export_row(row), ensure_ascii=False) + "\n").encode("utf-8")
        if len(rows) < HISTORY_EXPORT_PAGE_SIZE:
            return
        offset += HISTORY_EXPORT_PAGE_SIZE


@router.get("/history/export")
async def export_alert_history(
    team_id: int | None = Query(default=None),
    cluster_id: list[int] = Query(default=[]),
    status_filter: Literal["firing", "resolved"] | None = Query(default=None, alias="status"),
    severity: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    search: str | None = Query(default=None),
    from_ts: datetime | None = Query(default=None),
    to_ts: datetime | None = Query(default=None),
    include_test: bool = Query(default=False),
    export_format: Literal["json", "ndjson"] = Query(default="json", alias="format"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The same scoping/filters as GET /history, exported whole rather than
    paginated -- `json` (small, capped, one document) or `ndjson` (larger cap,
    genuinely streamed).
    """
    team = await _resolve_team_scope(team_id, user, session)
    conditions = _build_history_conditions(
        team=team,
        cluster_id=cluster_id,
        status_filter=status_filter,
        severity=severity,
        namespace=namespace,
        search=search,
        from_ts=from_ts,
        to_ts=to_ts,
        include_test=include_test,
    )

    filters = {
        "team_id": team.id if team is not None else None,
        "cluster_id": cluster_id or None,
        "status": status_filter,
        "severity": severity,
        "namespace": namespace,
        "search": search,
        "from_ts": from_ts.isoformat() if from_ts else None,
        "to_ts": to_ts.isoformat() if to_ts else None,
        "include_test": include_test,
    }

    cap = HISTORY_EXPORT_JSON_CAP if export_format == "json" else HISTORY_EXPORT_NDJSON_CAP
    total = (
        await session.execute(select(func.count()).select_from(AlertEvent).where(*conditions))
    ).scalar_one()
    if total > cap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"필터를 좁혀주세요: {total}건이 캡({cap}건)을 초과했습니다",
        )

    await audit.log(
        session,
        user_id=user.id,
        team_id=team.id if team is not None else None,
        action="history.export",
        object_type="alert_event",
        object_ref="history",
        detail={"filters": filters, "format": export_format},
    )
    await session.commit()

    if export_format == "ndjson":
        return StreamingResponse(
            _stream_history_ndjson(session, conditions),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="kam-alert-history.ndjson"'},
        )

    result = await session.execute(
        select(AlertEvent)
        .where(*conditions)
        .order_by(AlertEvent.last_received_at.desc(), AlertEvent.id.desc())
    )
    payload = {
        "kam_export_version": 1,
        "kind": "alert_history",
        "exported_at": datetime.now(UTC).isoformat(),
        "filters": filters,
        "items": [_export_row(row) for row in result.scalars().all()],
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="kam-alert-history.json"'},
    )


async def _get_event_or_404(session: AsyncSession, event_id: int) -> AlertEvent:
    event = await session.get(AlertEvent, event_id)
    if event is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return event


async def _authorize_event_access(session: AsyncSession, event: AlertEvent, user: User) -> None:
    if user.is_admin:
        return
    if event.team_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == event.team_id, TeamMembership.user_id == user.id
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


@router.get("/history/{event_id}")
async def get_alert_history_detail(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)
    return await _serialize_one_detail(session, event)


@router.get("/history/{event_id}/notifications")
async def get_alert_history_notifications(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Delivery history for one alert event: the outbox row per channel it
    was routed to (or would-be-routed to before delivery), for the alert
    history detail drawer's "notification history" section.
    """
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)

    result = await session.execute(
        select(NotificationOutbox, Channel.name)
        .join(Channel, Channel.id == NotificationOutbox.channel_id)
        .where(NotificationOutbox.alert_event_id == event_id)
        .order_by(NotificationOutbox.created_at)
    )
    return [
        {
            "id": outbox.id,
            "channel_id": outbox.channel_id,
            "channel_name": channel_name,
            "trigger": outbox.trigger,
            "status": outbox.status,
            "attempts": outbox.attempts,
            "last_error": outbox.last_error,
            "created_at": outbox.created_at,
            "delivered_at": outbox.delivered_at,
        }
        for outbox, channel_name in result.all()
    ]


# -- ack / assignee -------------------------------------------------------


@router.post("/history/{event_id}/ack")
async def ack_alert(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Acknowledge one alert event.

    Idempotent: a second ack call on an already-acknowledged event is a
    no-op 200 that returns the existing ack unchanged, rather than letting
    a later caller silently overwrite who/when acknowledged it first.
    """
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)

    if event.acknowledged_at is None:
        event.acknowledged_at = datetime.now(UTC)
        event.acknowledged_by = user.id
        await audit.log(
            session,
            user_id=user.id,
            team_id=event.team_id,
            action="alert.ack",
            object_type="alert_event",
            object_ref=str(event.id),
        )
        await session.commit()
        await session.refresh(event)

    return await _serialize_one_detail(session, event)


@router.delete("/history/{event_id}/ack")
async def unack_alert(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)

    if event.acknowledged_at is not None:
        event.acknowledged_at = None
        event.acknowledged_by = None
        await audit.log(
            session,
            user_id=user.id,
            team_id=event.team_id,
            action="alert.unack",
            object_type="alert_event",
            object_ref=str(event.id),
        )
        await session.commit()
        await session.refresh(event)

    return await _serialize_one_detail(session, event)


class AssigneeUpdate(BaseModel):
    user_id: int | None = None


async def _validate_assignee(
    session: AsyncSession, team_id: int | None, user_id: int | None
) -> None:
    if user_id is None:
        return
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="unknown user_id"
        )
    if target.is_admin:
        return
    if team_id is not None:
        result = await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
            )
        )
        if result.scalar_one_or_none() is not None:
            return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="user_id must be a member of this event's team (or an admin)",
    )


@router.put("/history/{event_id}/assignee")
async def set_alert_assignee(
    event_id: int,
    body: AssigneeUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)
    await _validate_assignee(session, event.team_id, body.user_id)

    event.assignee_user_id = body.user_id
    await audit.log(
        session,
        user_id=user.id,
        team_id=event.team_id,
        action="alert.assign",
        object_type="alert_event",
        object_ref=str(event.id),
        detail={"assignee_user_id": body.user_id},
    )
    await session.commit()
    await session.refresh(event)

    return await _serialize_one_detail(session, event)


# -- comments ---------------------------------------------------------------


class CommentCreate(BaseModel):
    body: str

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("body must not be blank")
        if len(stripped) > MAX_COMMENT_LENGTH:
            raise ValueError(f"body must be {MAX_COMMENT_LENGTH} characters or fewer")
        return stripped


def _serialize_comment(comment: AlertComment, author: User | None) -> dict[str, Any]:
    return {
        "id": comment.id,
        "user": (
            {"id": author.id, "username": author.username, "display_name": author.display_name}
            if author is not None
            else None
        ),
        "body": comment.body,
        "created_at": comment.created_at,
    }


@router.get("/history/{event_id}/comments")
async def list_alert_comments(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)

    result = await session.execute(
        select(AlertComment, User)
        .outerjoin(User, User.id == AlertComment.user_id)
        .where(AlertComment.alert_event_id == event_id)
        .order_by(AlertComment.created_at)
    )
    return [_serialize_comment(comment, author) for comment, author in result.all()]


@router.post("/history/{event_id}/comments", status_code=status.HTTP_201_CREATED)
async def create_alert_comment(
    event_id: int,
    body: CommentCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)

    comment = AlertComment(alert_event_id=event_id, user_id=user.id, body=body.body)
    session.add(comment)
    await session.flush()

    await audit.log(
        session,
        user_id=user.id,
        team_id=event.team_id,
        action="alert.comment",
        object_type="alert_comment",
        object_ref=str(comment.id),
        detail={"alert_event_id": event_id},
    )
    await session.commit()
    await session.refresh(comment)

    return _serialize_comment(comment, user)


comments_router = APIRouter(prefix="/api/v1/comments", tags=["alerts"])


@comments_router.delete("/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert_comment(
    comment_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Deletable by the comment's own author, the owner of the team the
    comment's alert event belongs to, or an admin.
    """
    result = await session.execute(
        select(AlertComment, AlertEvent.team_id)
        .join(AlertEvent, AlertEvent.id == AlertComment.alert_event_id)
        .where(AlertComment.id == comment_id)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="comment not found")
    comment, team_id = row

    allowed = user.is_admin or comment.user_id == user.id
    if not allowed and team_id is not None:
        membership_result = await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == user.id,
                TeamMembership.role == "owner",
            )
        )
        allowed = membership_result.scalar_one_or_none() is not None
    if not allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    await audit.log(
        session,
        user_id=user.id,
        team_id=team_id,
        action="alert.comment.delete",
        object_type="alert_comment",
        object_ref=str(comment_id),
    )
    await session.delete(comment)
    await session.commit()


# -- test alerts ------------------------------------------------------------


async def _get_team_or_404(session: AsyncSession, team_id: int) -> Team:
    team = await session.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="team not found")
    return team


class TestAlertCreate(BaseModel):
    cluster_id: int
    alertname: str = "KamTestAlert"
    severity: str = "warning"
    namespace: str | None = None
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}


team_router = APIRouter(prefix="/api/v1/teams/{team_id}", tags=["alerts"])


@team_router.post("/test-alert")
async def fire_test_alert(
    team_id: int,
    body: TestAlertCreate,
    actor: User = Depends(require_team_role("member")),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Fire a synthetic alert through the real ingest+routing pipeline --
    `ingest_webhook` is called directly (not the webhook HTTP endpoint;
    auth here is already the caller's JWT session) -- so a team can see
    exactly how their current routing rules react without waiting for a
    real incident.

    `is_test=True` is set on the row right after ingest creates it rather
    than threaded through `ingest_webhook`'s signature: routing has no
    notion of test vs. real events by design (testing means exercising the
    *actual* rules), so marking it post-creation, before this same
    transaction commits, is sufficient.
    """
    team = await _get_team_or_404(session, team_id)
    cluster = await session.get(Cluster, body.cluster_id)
    if cluster is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="cluster not found")

    fingerprint = f"test-{uuid.uuid4().hex[:12]}"
    starts_at = datetime.now(UTC)
    labels = {
        **body.labels,
        "alertname": body.alertname,
        "severity": body.severity,
        "kam_team": team.slug,
        "kam_test": "true",
    }
    if body.namespace:
        labels["namespace"] = body.namespace

    payload = AlertmanagerWebhookPayload(
        alerts=[
            AlertmanagerAlert(
                status="firing",
                labels=labels,
                annotations=body.annotations,
                startsAt=starts_at.isoformat(),
                fingerprint=fingerprint,
            )
        ]
    )
    await ingest_webhook(session, cluster, payload)

    event = (
        await session.execute(
            select(AlertEvent).where(
                AlertEvent.cluster_id == cluster.id,
                AlertEvent.fingerprint == fingerprint,
                AlertEvent.starts_at == starts_at,
            )
        )
    ).scalar_one()
    event.is_test = True
    await session.flush()

    rules_result = await session.execute(
        select(RoutingRule)
        .where(RoutingRule.team_id == team_id, RoutingRule.enabled.is_(True))
        .options(selectinload(RoutingRule.matchers), selectinload(RoutingRule.channels))
    )
    verdicts = [
        {
            "rule_id": rule.id,
            "rule_name": rule.name,
            "action": rule.action,
            "verdict": evaluate(event, rule, rule.matchers, trigger="firing").kind.value,
        }
        for rule in rules_result.scalars().all()
    ]

    channels_result = await session.execute(
        select(Channel.name)
        .join(NotificationOutbox, NotificationOutbox.channel_id == Channel.id)
        .where(
            NotificationOutbox.alert_event_id == event.id, NotificationOutbox.trigger == "firing"
        )
    )
    delivered_channels = [name for (name,) in channels_result.all()]

    # route_event (already run, inside ingest_webhook's transition hook)
    # short-circuits entirely on the first matching suppress rule: zero
    # outbox rows get staged for *any* notify rule, even ones this
    # per-rule verdict loop above independently reports as "matched" (it
    # has no visibility into route_event's suppress-wins-exclusively
    # semantics). Surfacing which rule suppressed it lets the UI flag
    # those matched-but-not-delivered verdicts instead of presenting them
    # as if they'd actually notified.
    suppressed_by = None
    if event.suppressed_by_rule_id is not None:
        suppressing_rule = await session.get(RoutingRule, event.suppressed_by_rule_id)
        if suppressing_rule is not None:
            suppressed_by = {"rule_id": suppressing_rule.id, "rule_name": suppressing_rule.name}

    await audit.log(
        session,
        user_id=actor.id,
        team_id=team_id,
        action="alert.test_fire",
        object_type="alert_event",
        object_ref=str(event.id),
        detail={"cluster_id": cluster.id, "alertname": body.alertname, "fingerprint": fingerprint},
    )
    await session.commit()

    return {
        "event_id": event.id,
        "verdicts": verdicts,
        "delivered_channels": delivered_channels,
        "suppressed_by": suppressed_by,
    }


@router.post("/history/{event_id}/resolve-test")
async def resolve_test_alert(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Manually resolve a test alert (POST .../test-alert rows only -- 422
    on any other row), using the same firing->resolved transition
    semantics as a real Alertmanager resolved webhook: status flips,
    ends_at is stamped, and 'resolved' routing runs so the team sees the
    same notify_on_resolved behavior a real alert would trigger.

    No-ops (200, no re-routing) if the event is already resolved -- this is
    a transition endpoint, not a status setter, so there's nothing to
    transition. A scheduler-driven auto-resolve of stale test alerts is
    Phase 15 scope, not this one.
    """
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_access(session, event, user)

    if not event.is_test:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="resolve-test only applies to test alerts (is_test=True)",
        )

    if event.status == "firing":
        event.status = "resolved"
        event.ends_at = datetime.now(UTC)
        await route_event(session, event, "resolved")
        await audit.log(
            session,
            user_id=user.id,
            team_id=event.team_id,
            action="alert.test_resolve",
            object_type="alert_event",
            object_ref=str(event.id),
        )
        await session.commit()
        await session.refresh(event)

    return await _serialize_one_detail(session, event)


# -- live ack-status batch ----------------------------------------------


class AckStatusItem(BaseModel):
    cluster: str
    fingerprint: str


class AckStatusRequest(BaseModel):
    items: list[AckStatusItem]


@router.post("/ack-status")
async def get_ack_status(
    body: AckStatusRequest,
    team_id: int | None = Query(default=None),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Batch-resolve (cluster_name, fingerprint) pairs from the live-alerts
    view to their currently-open alert_events row's ack/assignee state, so
    the Alerts page can show an ack badge without a per-row round trip.

    Team-scoped exactly like /live and /history: a pair belonging to a
    team other than the requested (or the caller's) scope is silently
    absent from `matched`, not an error.
    """
    team = await _resolve_team_scope(team_id, user, session)

    if not body.items:
        return {"matched": []}

    wanted = {(item.cluster, item.fingerprint) for item in body.items}
    fingerprints = {item.fingerprint for item in body.items}

    conditions = [AlertEvent.status == "firing", AlertEvent.fingerprint.in_(fingerprints)]
    if team is not None:
        conditions.append(AlertEvent.team_id == team.id)

    result = await session.execute(
        select(AlertEvent).where(*conditions).order_by(AlertEvent.starts_at.desc())
    )
    events = result.scalars().all()

    usernames = await _resolve_usernames(session, {e.assignee_user_id for e in events})

    matched: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        key = (event.cluster_name, event.fingerprint)
        if key not in wanted or key in seen:
            continue
        seen.add(key)
        matched.append(
            {
                "cluster": event.cluster_name,
                "fingerprint": event.fingerprint,
                "event_id": event.id,
                "acknowledged": event.acknowledged_at is not None,
                "assignee_username": (
                    usernames.get(event.assignee_user_id)
                    if event.assignee_user_id is not None
                    else None
                ),
            }
        )

    return {"matched": matched}
