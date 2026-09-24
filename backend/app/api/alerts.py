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
from sqlalchemy import ColumnElement, and_, func, or_, select
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
from app.models.share import AlertShare
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
from app.services.scheduled_actions import cancel_pending
from app.services.sharing import (
    CompiledShareScope,
    MatchableAlert,
    compile_share_scope,
    scope_matches,
    share_matches,
    shared_source_team_ids,
)

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
        # Phase 14: set below, in get_live_alerts, once the requested
        # team's shared sources are known -- None here is just the default
        # for an alert that turns out to belong to the requested team
        # itself (or for the unscoped admin view, where sharing doesn't
        # apply at all).
        "shared_from": None,
    }


async def _load_shared_scopes_by_slug(
    session: AsyncSession, team_id: int
) -> dict[str, CompiledShareScope]:
    """owner team slug -> that owner's `AlertShare` scope, pre-compiled once
    (view or view_notify -- both grant read visibility; view_notify's extra
    notify-side effect is handled entirely by `route_event`). Keyed by slug
    rather than id since callers here match against a `kam_team` label /
    denormalized team slug, not an id.

    Compiling here (once per request) rather than calling `share_matches`
    per alert matters: `/live` can be checking this against every alert
    across every enabled cluster, so recompiling the same share's patterns
    on every single one would be pure waste.
    """
    pairs = await shared_source_team_ids(session, team_id)
    if not pairs:
        return {}
    owner_ids = [owner_id for owner_id, _ in pairs]
    result = await session.execute(select(Team).where(Team.id.in_(owner_ids)))
    slug_by_id = {t.id: t.slug for t in result.scalars().all()}
    return {
        slug_by_id[owner_id]: compile_share_scope(share)
        for owner_id, share in pairs
        if owner_id in slug_by_id
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
        # Phase 14: a non-owner alert is included only if some AlertShare
        # targeting this team covers it (owner slug matches a share, and
        # that share's optional matcher scope -- see scope_matches --
        # accepts this alert). `shared_from` records which owner it came
        # through, for the "공유: {owner}" badge; own-team alerts keep the
        # `_flatten` default of None.
        shared_scopes = await _load_shared_scopes_by_slug(session, team.id)
        scoped: list[dict[str, Any]] = []
        for alert in alerts:
            owner_slug = alert["labels"].get("kam_team")
            if owner_slug == team.slug:
                scoped.append(alert)
                continue
            scope = shared_scopes.get(owner_slug) if owner_slug else None
            if scope is not None and scope_matches(scope, MatchableAlert.from_live_alert(alert)):
                alert["shared_from"] = owner_slug
                scoped.append(alert)
        alerts = scoped

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


def _serialize_event_summary(
    event: AlertEvent, usernames: dict[int, str], *, shared_from: str | None = None
) -> dict[str, Any]:
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
        # Phase 14: the owner team's slug when this event reached the
        # viewer only via an AlertShare, None for the viewer's own team's
        # events (and always None outside a team-scoped context).
        "shared_from": shared_from,
    }


def _serialize_event_detail(
    event: AlertEvent,
    usernames: dict[int, str],
    grafana_url: str | None,
    *,
    shared_from: str | None = None,
) -> dict[str, Any]:
    return {
        **_serialize_event_summary(event, usernames, shared_from=shared_from),
        "labels": event.labels,
        "annotations": event.annotations,
        "generator_url": event.generator_url,
        "grafana_url": grafana_url,
    }


async def _resolve_shared_from(
    session: AsyncSession, event: AlertEvent, viewer: User
) -> str | None:
    """None when `viewer` is an admin or belongs to the event's own team --
    the event isn't "shared" from their point of view. Otherwise (reachable
    only because `_authorize_event_read_access` already granted access via
    a share) resolves the event's own team's slug, so the detail drawer can
    show the same "공유: {owner}" badge /live and /history do.
    """
    if viewer.is_admin or event.team_id is None:
        return None
    result = await session.execute(
        select(TeamMembership).where(
            TeamMembership.team_id == event.team_id, TeamMembership.user_id == viewer.id
        )
    )
    if result.scalar_one_or_none() is not None:
        return None
    team = await session.get(Team, event.team_id)
    return team.slug if team is not None else None


async def _serialize_one_detail(
    session: AsyncSession, event: AlertEvent, *, viewer: User | None = None
) -> dict[str, Any]:
    """Convenience for endpoints returning exactly one event (ack/assignee/
    resolve-test/detail) -- resolves just that event's own ack/assignee
    usernames rather than pulling in `_resolve_usernames`' page-batching for
    a single row, plus its Grafana deep link (annotation, falling back to
    the owning cluster's `grafana_url` -- see `resolve_grafana_url`).

    `viewer` is only passed by the GET detail endpoint (the only caller
    that needs `shared_from` -- every mutation endpoint using this already
    requires own-team membership via `_authorize_event_access`, so
    `shared_from` would always be None for them anyway).
    """
    usernames = await _resolve_usernames(session, {event.acknowledged_by, event.assignee_user_id})
    cluster = await session.get(Cluster, event.cluster_id)
    grafana_url = resolve_grafana_url(event.annotations, cluster, event.alertname)
    shared_from = await _resolve_shared_from(session, event, viewer) if viewer is not None else None
    return _serialize_event_detail(event, usernames, grafana_url, shared_from=shared_from)


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
    shared_owner_team_ids: list[int] | None = None,
) -> list[ColumnElement[bool]]:
    """Shared by GET /history (paginated) and GET /history/export (the whole
    matching set) -- every filter must behave identically between the two.

    `shared_owner_team_ids` (Phase 14, GET /history only -- export doesn't
    pass it, so its behavior is unchanged) widens the team_id condition from
    "only `team.id`" to "`team.id` OR any of these owner teams' events".
    This only covers a share's *team* scope, not its optional matcher scope
    -- a matcher can't be expressed as a WHERE clause here (it reads
    alertname/labels/annotations, not an indexed column), so a
    matcher-scoped share needs a Python post-fetch filter on top of this;
    see `get_alert_history`'s docstring for the pagination trade-off that
    implies.
    """
    conditions: list[ColumnElement[bool]] = []
    if not include_test:
        conditions.append(AlertEvent.is_test.is_(False))
    if team is not None:
        if shared_owner_team_ids:
            conditions.append(AlertEvent.team_id.in_([team.id, *shared_owner_team_ids]))
        else:
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

    Phase 14: when `team` has been shared alerts by other teams, this widens
    to also include (matcher-scoped) events owned by those teams --
    `shared_from` on each returned item names which owner a non-own-team row
    came through. A share with no matchers is fully expressed in the SQL
    WHERE clause (`_build_history_conditions`'s `shared_owner_team_ids`); a
    share *with* matchers additionally needs a Python post-fetch filter,
    since a matcher reads alertname/labels/annotations, not a column SQL can
    filter on. That post-filter runs *after* `LIMIT`/`OFFSET`, so `total`
    and a page's row count are an approximation (an upper bound) whenever a
    matcher-scoped share is in play -- a page can come back with fewer than
    `page_size` rows, or `total` can overcount what a user would see if they
    paged all the way through. This is an accepted approximation for this
    phase rather than re-deriving pagination from a filter-then-count pass.
    """
    team = await _resolve_team_scope(team_id, user, session)

    # Pre-compiled once per share here (not per event below) -- a page can
    # hold up to 200 rows, and re-deriving the same share's compiled
    # matchers for every one of them would be pure waste.
    shared_scopes: dict[int, CompiledShareScope] = {}
    owner_slug_by_id: dict[int, str] = {}
    if team is not None:
        pairs = await shared_source_team_ids(session, team.id)
        if pairs:
            shared_scopes = {owner_id: compile_share_scope(share) for owner_id, share in pairs}
            owners = (
                await session.execute(select(Team).where(Team.id.in_(shared_scopes)))
            ).scalars().all()
            owner_slug_by_id = {t.id: t.slug for t in owners}

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
        shared_owner_team_ids=list(shared_scopes) or None,
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

    if team is not None and shared_scopes:
        items = [
            e
            for e in items
            if e.team_id == team.id or scope_matches(shared_scopes[e.team_id], e)
        ]

    usernames = await _resolve_usernames(
        session, {e.acknowledged_by for e in items} | {e.assignee_user_id for e in items}
    )

    def _shared_from(event: AlertEvent) -> str | None:
        if team is None or event.team_id == team.id:
            return None
        return owner_slug_by_id.get(event.team_id)

    return {
        "items": [
            _serialize_event_summary(e, usernames, shared_from=_shared_from(e)) for e in items
        ],
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
    """Page through every matching row via keyset (not offset) pagination,
    and never emit more than HISTORY_EXPORT_NDJSON_CAP rows.

    Keyset, not offset: `last_received_at` mutates in place on a re-fire
    (see AlertEvent's docstring), so under live ingestion a row can shift
    across an *offset* boundary between two page queries -- silently
    skipping or double-emitting rows depending on which way it moved.
    Paging instead by "strictly before the last row we emitted" in the same
    (last_received_at DESC, id DESC) order as the output is immune to that:
    a row already emitted can't un-emit itself just because some other row's
    timestamp changed, and a row not yet reached is simply wherever the next
    query finds it.

    Hard cap, not just the pre-flight COUNT(*) gate: that count and this
    scan are two separate queries, so the live matching-row total can have
    grown past HISTORY_EXPORT_NDJSON_CAP by the time this actually runs (or
    simply differ from it for the same reason a row can move across an
    offset boundary above). This loop refuses to yield past the cap
    regardless of how many rows actually match, rather than trusting the
    earlier count to still be accurate.
    """
    cursor: tuple[datetime, int] | None = None
    emitted = 0
    while emitted < HISTORY_EXPORT_NDJSON_CAP:
        page_conditions = list(conditions)
        if cursor is not None:
            last_value, last_id = cursor
            # Explicit OR/AND expansion of the (last_received_at, id) < (v, i)
            # tuple comparison rather than SQLAlchemy's tuple_(...) < (...):
            # row-value comparison is SQLite-version-dependent (3.15+) but
            # this expansion is portable and reads identically on Postgres.
            page_conditions.append(
                or_(
                    AlertEvent.last_received_at < last_value,
                    and_(
                        AlertEvent.last_received_at == last_value,
                        AlertEvent.id < last_id,
                    ),
                )
            )

        page_limit = min(HISTORY_EXPORT_PAGE_SIZE, HISTORY_EXPORT_NDJSON_CAP - emitted)
        result = await session.execute(
            select(AlertEvent)
            .where(*page_conditions)
            .order_by(AlertEvent.last_received_at.desc(), AlertEvent.id.desc())
            .limit(page_limit)
        )
        rows = result.scalars().all()
        if not rows:
            return

        for row in rows:
            yield (json.dumps(_export_row(row), ensure_ascii=False) + "\n").encode("utf-8")
        emitted += len(rows)
        cursor = (rows[-1].last_received_at, rows[-1].id)

        if len(rows) < page_limit:
            return


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
    """Mutation-grade authorization: admin, or a member of the event's own
    team. Every write endpoint (ack/unack, assignee, comment create/delete,
    resolve-test) uses this, unchanged by Phase 14 -- sharing only ever
    grants read (and, for view_notify, a *separate* team's own notify) on
    another team's alert, never write access to it. See
    `_authorize_event_read_access` for the read-only variant those
    endpoints don't use.
    """
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


async def _authorize_event_read_access(
    session: AsyncSession, event: AlertEvent, user: User
) -> set[int] | None:
    """Read-only variant of `_authorize_event_access` (Phase 14): additionally
    allows a member of a team that `event`'s own team has shared this event
    into -- a `view`/`view_notify` `AlertShare` whose matcher scope covers
    it. Used only by the three read-only detail-drawer endpoints (detail,
    notification history, comment list); every mutation endpoint keeps
    using `_authorize_event_access` unchanged, so ack/assignee/comment-
    create/comment-delete/resolve-test stay 403 for a shared-in viewer.

    Returns `None` when access is unrestricted -- admin, or a genuine member
    of the event's own team -- meaning the caller may show everything
    belonging to this event, same as before Phase 14. Returns the viewer's
    own team id set when access was granted *only* via a share:
    `GET .../notifications` uses this to scope which `NotificationOutbox`
    rows (and therefore which OTHER teams' channel names/delivery errors) a
    shared-in viewer may see -- only rows attributed to a team they're
    actually a member of, never the owning team's own or another target
    team's.
    """
    if user.is_admin:
        return None
    if event.team_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    result = await session.execute(
        select(TeamMembership.team_id).where(TeamMembership.user_id == user.id)
    )
    member_team_ids = {team_id for (team_id,) in result.all()}
    if event.team_id in member_team_ids:
        return None

    if member_team_ids:
        shares_result = await session.execute(
            select(AlertShare).where(
                AlertShare.owner_team_id == event.team_id,
                AlertShare.target_team_id.in_(member_team_ids),
            )
        )
        if any(share_matches(share, event) for share in shares_result.scalars().all()):
            return member_team_ids

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")


@router.get("/history/{event_id}")
async def get_alert_history_detail(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    event = await _get_event_or_404(session, event_id)
    await _authorize_event_read_access(session, event, user)
    return await _serialize_one_detail(session, event, viewer=user)


@router.get("/history/{event_id}/notifications")
async def get_alert_history_notifications(
    event_id: int,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Delivery history for one alert event: the outbox row per channel it
    was routed to (or would-be-routed to before delivery), for the alert
    history detail drawer's "notification history" section.

    Phase 14: `route_event`'s view_notify fan-out can stage outbox rows for
    OTHER teams too (a share's target, notified through its own channels).
    A viewer whose access came only from a share (not their own team's
    membership, per `_authorize_event_read_access`'s return value) sees
    only the rows attributed to a team they're actually a member of --
    never the owning team's own channel names/delivery errors, nor another
    target team's. A genuine member of the event's own team (or an admin)
    sees every row for this event, unrestricted, same as before Phase 14.
    """
    event = await _get_event_or_404(session, event_id)
    restrict_to_team_ids = await _authorize_event_read_access(session, event, user)

    conditions: list[ColumnElement[bool]] = [NotificationOutbox.alert_event_id == event_id]
    if restrict_to_team_ids is not None:
        conditions.append(NotificationOutbox.team_id.in_(restrict_to_team_ids))

    result = await session.execute(
        select(NotificationOutbox, Channel.name)
        .join(Channel, Channel.id == NotificationOutbox.channel_id)
        .where(*conditions)
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
        # Phase 15: an acknowledged alert has nothing left to escalate about
        # (dispatch would cancel it anyway once it re-checks acknowledged_at,
        # but cancelling eagerly here means the admin/team UI reflects it
        # immediately instead of waiting for the escalation's due_at). Not
        # restored on unack (see unack_alert below) -- documented as a
        # deliberate simplification in this phase's brief.
        await cancel_pending(session, event.id)
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
    await _authorize_event_read_access(session, event, user)

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
            NotificationOutbox.alert_event_id == event.id,
            NotificationOutbox.trigger == "firing",
            # Phase 14: route_event's view_notify fan-out can stage outbox
            # rows for OTHER teams too (a share's target, notified through
            # its own channels). This response is specifically "what did
            # MY team's own routing rules deliver" -- scoped back to this
            # team's own rows, same as `rules_result`/`verdicts` above.
            NotificationOutbox.team_id == team_id,
        )
    )
    delivered_channels = [name for (name,) in channels_result.all()]

    # route_event's suppress rules short-circuit exclusively for THIS
    # (owning) team: a matching suppress rule blocks every notify rule's
    # channels for this team, even ones this per-rule verdict loop above
    # independently reports as "matched" (it has no visibility into
    # route_event's suppress-wins-exclusively semantics). Surfacing which
    # rule suppressed it lets the UI flag those matched-but-not-delivered
    # verdicts instead of presenting them as if they'd actually notified.
    #
    # This -- and `delivered_channels` above -- is deliberately scoped to
    # the owning team only: `suppressed_by` can coexist with a completely
    # independent view_notify delivery to a target team this team shares
    # with (see route_event's docstring -- a target's own routing, and the
    # owner's own suppress, never affect each other). That cross-team
    # delivery is invisible in this response by design, since every field
    # here answers "what happened from THIS team's own point of view".
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

    Team-scoped exactly like /live and /history -- including Phase 14's
    same matcher-scoped widen to alerts shared into `team` by another
    team's view/view_notify AlertShare, since /live's own listing now
    includes those too (a shared row with no ack badge would otherwise look
    broken). A pair belonging to a team outside that scope is silently
    absent from `matched`, not an error; the response carries no team
    attribution of its own (cluster/fingerprint/event_id/ack/assignee only)
    since the caller already knows which row is whose from /live's
    `shared_from`.
    """
    team = await _resolve_team_scope(team_id, user, session)

    if not body.items:
        return {"matched": []}

    wanted = {(item.cluster, item.fingerprint) for item in body.items}
    fingerprints = {item.fingerprint for item in body.items}

    conditions: list[ColumnElement[bool]] = [
        AlertEvent.status == "firing",
        AlertEvent.fingerprint.in_(fingerprints),
    ]
    shared_scopes: dict[int, CompiledShareScope] = {}
    if team is not None:
        pairs = await shared_source_team_ids(session, team.id)
        if pairs:
            shared_scopes = {owner_id: compile_share_scope(share) for owner_id, share in pairs}
            conditions.append(AlertEvent.team_id.in_([team.id, *shared_scopes]))
        else:
            conditions.append(AlertEvent.team_id == team.id)

    result = await session.execute(
        select(AlertEvent).where(*conditions).order_by(AlertEvent.starts_at.desc())
    )
    events = result.scalars().all()
    if team is not None and shared_scopes:
        events = [
            e
            for e in events
            if e.team_id == team.id or scope_matches(shared_scopes[e.team_id], e)
        ]

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
