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
    # Optional despite AM always sending it in practice: a malformed or
    # missing startsAt must degrade to skipping that one alert (see
    # `_ingest_one`), not a 400 for the whole batch.
    startsAt: str | None = None
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
    reopened: int = 0
    repeats: int = 0
    heartbeats_seen: int = 0
    skipped: int = 0


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
    """Called on a brand-new firing event, a firing->resolved transition,
    and a resolved->firing re-open (never on a resolved-first insert or a
    repeat with no status change).

    Contract: this is called INSIDE the ingest transaction, pre-commit.
    Implementations must only stage DB work here (e.g. outbox inserts) --
    never dispatch externally -- since the whole transaction (and this
    call along with it) can still be rolled back by a later failure in the
    same batch.

    Phase 9: 라우팅/outbox 연결점 -- notification dispatch will hang off
    this hook. No-op for now.
    """


async def _ingest_one(
    session: AsyncSession, cluster: Cluster, alert: AlertmanagerAlert, result: IngestResult
) -> None:
    """Process one alert from a webhook batch.

    Alerts are processed independently: a malformed or missing `startsAt`
    on one alert skips just that alert (logged, counted in
    `result.skipped`) rather than raising out of the whole batch --
    Alertmanager retries a failed delivery forever, so a single bad alert
    must never take the rest of a batch down with it. `startsAt` has no
    "treat as now" fallback (a prior version defaulted to `now`, which
    silently broke dedup by minting a fresh identity on every retry of the
    same undated alert); a usable identity requires a real `startsAt`, so
    a missing/zero one is always a skip, never a guess.
    """
    now = datetime.now(UTC)
    alertname = alert.labels.get("alertname", "")

    if cluster.heartbeat_enabled and alertname == cluster.heartbeat_alertname:
        cluster.last_heartbeat_at = now
        cluster.heartbeat_state = "ok"
        result.heartbeats_seen += 1
        return

    try:
        starts_at = _parse_am_timestamp(alert.startsAt)
    except ValueError:
        logger.warning(
            "skipping alert with malformed startsAt=%r for cluster=%s fingerprint=%s",
            alert.startsAt,
            cluster.id,
            alert.fingerprint,
        )
        result.skipped += 1
        return
    if starts_at is None:
        logger.warning(
            "skipping alert with missing/zero-value startsAt for cluster=%s fingerprint=%s",
            cluster.id,
            alert.fingerprint,
        )
        result.skipped += 1
        return

    try:
        ends_at = _parse_am_timestamp(alert.endsAt)
    except ValueError:
        # Unlike startsAt, a bad endsAt doesn't cost the alert its
        # identity -- there's still a well-formed event to record, just
        # without a known resolution time. `now()` would be actively
        # wrong here (it isn't when AM says the alert resolved), so this
        # degrades to "unresolved" (None) rather than guessing.
        logger.warning(
            "malformed endsAt=%r for cluster=%s fingerprint=%s; treating as unresolved",
            alert.endsAt,
            cluster.id,
            alert.fingerprint,
        )
        ends_at = None

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
    elif alert.status == "firing" and existing.status == "resolved":
        # Same identity firing again after having been resolved. AM
        # serializes per-group notifications, so a stale, out-of-order
        # firing-after-resolved delivery for the same (cluster,
        # fingerprint, starts_at) is rare in practice -- normally this
        # means the alert genuinely re-fired (or this is a duplicate
        # network retry of the firing webhook that raced the resolved one
        # and lost). Firing wins: re-open the row so Phase 9 notifies on
        # it, rather than leaving a resolved row that silently swallows a
        # real re-fire. Accepted trade-off: in the rare stale-retry case,
        # the row incorrectly reads "firing" until the next resolved
        # delivery corrects it.
        existing.status = "firing"
        existing.ends_at = None
        result.reopened += 1
        await on_event_transition(session, existing, "firing")
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
