"""Alertmanager webhook ingest: heartbeat handling, composite-identity
dedup, and firing/resolved transition tracking for `alert_events`.

The caller owns the transaction: `ingest_webhook` only adds/mutates ORM
objects and issues a SAVEPOINT around each insert attempt (to survive a
racing concurrent delivery of the same identity) -- it never commits or
rolls back the outer session.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import AlertEvent
from app.models.cluster import Cluster
from app.models.team import Team

logger = logging.getLogger(__name__)

# Alertmanager's Go zero-value timestamp, used for `endsAt` on alerts that
# haven't resolved yet -- semantically "no end time", i.e. None.
_AM_ZERO_TIME = "0001-01-01T00:00:00Z"


class AlertmanagerAlert(BaseModel):
    """One alert entry from an Alertmanager webhook v4 payload. Only the
    fields this app uses are declared; anything else is ignored so future
    AM fields don't break parsing.
    """

    model_config = ConfigDict(extra="ignore")

    status: str
    labels: dict[str, str] = {}
    annotations: dict[str, str] = {}
    startsAt: str
    endsAt: str | None = None
    fingerprint: str
    generatorURL: str | None = None


class AlertmanagerWebhookPayload(BaseModel):
    """Alertmanager webhook v4 top-level payload."""

    model_config = ConfigDict(extra="ignore")

    version: str | None = None
    groupKey: str | None = None
    status: str | None = None
    alerts: list[AlertmanagerAlert] = []


@dataclass
class IngestResult:
    received: int = 0
    created: int = 0
    created_resolved: int = 0
    resolved: int = 0
    repeats: int = 0
    heartbeats_seen: int = 0


def _parse_am_timestamp(value: str | None) -> datetime | None:
    """Parse an Alertmanager RFC3339 timestamp.

    Python 3.12's `datetime.fromisoformat` already accepts the 'Z' suffix
    and truncates sub-microsecond precision on its own, so nanosecond-
    precision AM timestamps parse fine as-is. The one thing that needs
    handling explicitly is AM's Go zero-value ("no end time"), which is a
    literal timestamp string rather than null.

    The result is always normalized to a UTC-aware datetime: AM sends an
    explicit offset (`Z` or `+HH:MM`), so `.astimezone(UTC)` here is a
    pure timezone conversion, not a "treat naive-as-local" guess. This is
    the one place identity- and display-relevant timestamps enter the
    system, so it's also the one place that needs to normalize them --
    without it, the same instant delivered as `...Z` on one webhook call
    and `...+09:00` on a retry would compare unequal and mint two rows for
    what's really the same alert (see `_get_existing`'s identity lookup).

    Raises `ValueError` (propagated from `fromisoformat`) if `value` is a
    non-empty string that isn't valid RFC3339 -- callers must handle a
    malformed timestamp explicitly rather than let it degrade to `None`.
    """
    if value is None or value == _AM_ZERO_TIME:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


async def _resolve_team_id(session: AsyncSession, slug: str | None) -> int | None:
    if not slug:
        return None
    result = await session.execute(select(Team.id).where(Team.slug == slug))
    return result.scalar_one_or_none()


async def _get_existing(
    session: AsyncSession, cluster_id: int, fingerprint: str, starts_at: datetime
) -> AlertEvent | None:
    result = await session.execute(
        select(AlertEvent).where(
            AlertEvent.cluster_id == cluster_id,
            AlertEvent.fingerprint == fingerprint,
            AlertEvent.starts_at == starts_at,
        )
    )
    return result.scalar_one_or_none()


async def on_event_transition(session: AsyncSession, event: AlertEvent, kind: str) -> None:
    """Called on a brand-new firing event and on a firing->resolved
    transition (never on a resolved-first insert or a repeat with no
    status change).

    Phase 9: 라우팅/outbox 연결점 -- notification dispatch will hang off
    this hook. No-op for now.
    """


async def _ingest_one(
    session: AsyncSession, cluster: Cluster, alert: AlertmanagerAlert, result: IngestResult
) -> None:
    now = datetime.now(UTC)
    alertname = alert.labels.get("alertname", "")

    if cluster.heartbeat_enabled and alertname == cluster.heartbeat_alertname:
        cluster.last_heartbeat_at = now
        cluster.heartbeat_state = "ok"
        result.heartbeats_seen += 1
        return

    starts_at = _parse_am_timestamp(alert.startsAt) or now
    ends_at = _parse_am_timestamp(alert.endsAt)
    team_id = await _resolve_team_id(session, alert.labels.get("kam_team"))
    raw_severity = alert.labels.get("severity")
    severity = raw_severity.lower() if raw_severity else None
    namespace = alert.labels.get("namespace") or None

    existing = await _get_existing(session, cluster.id, alert.fingerprint, starts_at)

    if existing is None:
        event = AlertEvent(
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            fingerprint=alert.fingerprint,
            status=alert.status,
            alertname=alertname,
            severity=severity,
            namespace=namespace,
            labels=alert.labels,
            annotations=alert.annotations,
            team_id=team_id,
            starts_at=starts_at,
            ends_at=ends_at if alert.status == "resolved" else None,
            generator_url=alert.generatorURL,
            first_received_at=now,
            last_received_at=now,
            receive_count=1,
        )
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            # A concurrent webhook delivery raced us to the same
            # (cluster_id, fingerprint, starts_at) identity and won -- the
            # UQ is the backstop here, so fall back to treating this as a
            # repeat of the row it just created.
            logger.info(
                "alert_events identity race for cluster=%s fingerprint=%s starts_at=%s; "
                "falling back to existing row",
                cluster.id,
                alert.fingerprint,
                starts_at,
            )
            # begin_nested()'s rollback-on-exception already detaches `event`
            # from the session (it was only ever pending, never committed).
            existing = await _get_existing(session, cluster.id, alert.fingerprint, starts_at)
            if existing is None:
                raise
        else:
            if alert.status == "firing":
                result.created += 1
                await on_event_transition(session, event, "firing")
            else:
                # A resolved alert with no prior firing row on record --
                # the firing webhook was likely missed (restart, network
                # blip). Still worth a history row, just never counted as
                # a "new firing" or run through the transition hook.
                result.created_resolved += 1
            return

    existing.last_received_at = now
    existing.receive_count += 1
    if alert.status == "resolved" and existing.status == "firing":
        existing.status = "resolved"
        existing.ends_at = ends_at
        result.resolved += 1
        await on_event_transition(session, existing, "resolved")
    else:
        result.repeats += 1


async def ingest_webhook(
    session: AsyncSession, cluster: Cluster, payload: AlertmanagerWebhookPayload
) -> IngestResult:
    result = IngestResult()
    for alert in payload.alerts:
        result.received += 1
        await _ingest_one(session, cluster, alert, result)
    return result
