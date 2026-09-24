"""Tests for Phase 16 (alert storm control): a channel's per-hour rate
limit + digest bundling.

Covers three layers:
- Staging (app.services.routing): the parking decision
  (_channel_needs_parking) and its trailing-hour count query, plus the
  single-pending-digest_flush guarantee (_ensure_digest_flush_scheduled) --
  exercised through the shared `stage_outbox_row` primitive and through
  `route_event` itself.
- Flush dispatch (app.worker.scheduler._dispatch_digest_flush): the 0/1/N
  parked-row cases, aggregate payload shape, and digested_into_id linking.
- Delivery (app/channels/base.py's default send_batch, EmailChannel's
  digest override, and app/worker/outbox.py's deliver() routing an
  is_digest row through send_batch instead of send()).

Also covers the brief's explicit constraint that escalation/renotify
staging respects the same per-channel parking decision as ordinary firing/
resolved staging (trigger-agnostic), and the UQ/NULL semantics that let
many digest aggregate rows coexist for one channel.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel
from sqlalchemy import select

import app.db as db_module
from app.channels.base import (
    AlertNotification,
    ChannelDeliveryError,
    NotificationChannel,
    RenderedMessage,
)
from app.channels.email import EmailChannel, EmailConfig
from app.channels.registry import ChannelRegistry
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.scheduled import ScheduledAction
from app.models.team import Team
from app.security import encrypt_str
from app.services.routing import route_event, stage_outbox_row
from app.worker.outbox import MAX_ATTEMPTS, deliver
from app.worker.scheduler import dispatch

# -- shared fixtures/helpers --------------------------------------------------


async def _create_team(session, slug: str = "platform") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(session, name: str = "storm-cluster") -> Cluster:
    cluster = Cluster(
        name=name,
        display_name=name,
        prometheus_url="http://prom",
        alertmanager_url="http://am",
        webhook_token_hash=f"hash-{name}",
    )
    session.add(cluster)
    await session.flush()
    return cluster


async def _create_channel(
    session,
    team: Team,
    *,
    name: str = "c1",
    rate_limit_per_hour: int | None = None,
    digest_mode: str = "off",
    digest_window_minutes: int = 5,
) -> Channel:
    channel = Channel(
        team_id=team.id,
        name=name,
        type="email",
        config_encrypted=encrypt_str(EmailConfig(recipients=["ops@example.org"]).model_dump_json()),
        rate_limit_per_hour=rate_limit_per_hour,
        digest_mode=digest_mode,
        digest_window_minutes=digest_window_minutes,
    )
    session.add(channel)
    await session.flush()
    return channel


async def _create_rule(
    session, team: Team, *, name: str = "r1", channels=None, escalation_channels=None, **fields
) -> RoutingRule:
    rule = RoutingRule(
        team_id=team.id,
        name=name,
        action="notify",
        channels=channels or [],
        escalation_channels=escalation_channels or [],
        **fields,
    )
    session.add(rule)
    await session.flush()
    return rule


_fp_counter = 0


async def _create_event(session, cluster: Cluster, team: Team, *, status: str = "firing") -> AlertEvent:
    global _fp_counter
    _fp_counter += 1
    event = AlertEvent(
        cluster_id=cluster.id,
        cluster_name=cluster.name,
        fingerprint=f"fp-{_fp_counter}",
        status=status,
        alertname="HighCpu",
        severity="critical",
        namespace="kam-demo",
        labels={"alertname": "HighCpu"},
        annotations={},
        team_id=team.id,
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(event)
    await session.flush()
    return event


def _example_payload(**overrides) -> dict:
    base = AlertNotification.example().model_dump(mode="json")
    base.update(overrides)
    return base


async def _insert_raw_outbox_row(
    session,
    channel: Channel,
    team: Team,
    *,
    created_at: datetime,
    status: str = "pending",
    is_digest: bool = False,
    trigger: str = "firing",
    alert_event_id: int | None = None,
) -> NotificationOutbox:
    """Insert a bare outbox row directly (bypassing route_event/
    stage_outbox_row) to set up a channel's trailing-hour history exactly as
    a test needs -- created_at in particular isn't settable through the
    normal staging path (it's a `default=`, not something callers pass).
    """
    row = NotificationOutbox(
        alert_event_id=alert_event_id,
        channel_id=channel.id,
        team_id=team.id,
        trigger=trigger,
        payload=_example_payload(),
        status=status,
        is_digest=is_digest,
        created_at=created_at,
    )
    session.add(row)
    await session.flush()
    return row


def _make_fake_channel_type(type_name: str = "fake-storm"):
    """Same factory pattern as tests/test_outbox_worker.py's -- a fresh,
    isolated fake channel class (default send_batch loop, NOT overridden)
    so digest delivery tests exercise the base class's per-item fallback.
    """
    sent: list[AlertNotification] = []
    fail_queue: list[Exception] = []

    class FakeConfig(BaseModel):
        marker: str = "ok"

    class FakeChannel(NotificationChannel):
        display_name = "Fake"
        config_schema = FakeConfig

        async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
            if fail_queue:
                raise fail_queue.pop(0)
            sent.append(notification)

    FakeChannel.type_name = type_name
    return FakeChannel, sent, fail_queue


def _registry_with(*channel_classes: type[NotificationChannel]) -> ChannelRegistry:
    registry = ChannelRegistry()
    registry.discover()
    for cls in channel_classes:
        registry._register(cls, source="test")
    return registry


async def _pending_digest_flush_actions(session, channel_id: int) -> list[ScheduledAction]:
    result = await session.execute(
        select(ScheduledAction).where(
            ScheduledAction.kind == "digest_flush",
            ScheduledAction.channel_id == channel_id,
            ScheduledAction.status == "pending",
        )
    )
    return list(result.scalars().all())


# -- parking decision matrix: off / auto (boundary) / always -----------------


async def test_digest_off_never_parks_regardless_of_volume(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        # rate_limit_per_hour set but digest_mode='off' -- must be ignored.
        channel = await _create_channel(session, team, digest_mode="off", rate_limit_per_hour=1)
        await _create_rule(session, team, channels=[channel])
        await session.commit()

        for _ in range(5):
            event = await _create_event(session, cluster, team)
            outcome = await route_event(session, event, "firing")
            assert outcome.routed is True
            await session.commit()

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 5
        assert all(r.status == "pending" for r in rows)
        assert await _pending_digest_flush_actions(session, channel.id) == []


async def test_digest_always_parks_unconditionally(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="always")
        await _create_rule(session, team, channels=[channel])
        event = await _create_event(session, cluster, team)
        await session.commit()

        outcome = await route_event(session, event, "firing")
        await session.commit()

        assert outcome.routed is True  # parked is still "routed"
        row = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert row.status == "digested"
        assert row.is_digest is False
        pending = await _pending_digest_flush_actions(session, channel.id)
        assert len(pending) == 1
        assert pending[0].due_at > datetime.now(UTC) + timedelta(minutes=4)


async def test_auto_mode_does_not_park_below_rate_limit(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="auto", rate_limit_per_hour=3)
        await _create_rule(session, team, channels=[channel])
        now = datetime.now(UTC)
        # Two prior sends this hour -- one below the limit of 3.
        await _insert_raw_outbox_row(session, channel, team, created_at=now - timedelta(minutes=10))
        await _insert_raw_outbox_row(session, channel, team, created_at=now - timedelta(minutes=5))
        event = await _create_event(session, cluster, team)
        await session.commit()

        await route_event(session, event, "firing")
        await session.commit()

        new_row = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event.id)
            )
        ).scalar_one()
        assert new_row.status == "pending"
        assert await _pending_digest_flush_actions(session, channel.id) == []


async def test_auto_mode_parks_exactly_at_rate_limit_boundary(app) -> None:
    """The count is >= (not >) the limit -- the row that would be the
    (limit+1)th send in the trailing hour is the first one parked.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="auto", rate_limit_per_hour=3)
        await _create_rule(session, team, channels=[channel])
        now = datetime.now(UTC)
        for minutes_ago in (30, 20, 10):
            await _insert_raw_outbox_row(session, channel, team, created_at=now - timedelta(minutes=minutes_ago))
        event = await _create_event(session, cluster, team)
        await session.commit()

        await route_event(session, event, "firing")
        await session.commit()

        new_row = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event.id)
            )
        ).scalar_one()
        assert new_row.status == "digested"
        assert len(await _pending_digest_flush_actions(session, channel.id)) == 1


async def test_auto_mode_with_no_rate_limit_configured_never_parks(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="auto", rate_limit_per_hour=None)
        await _create_rule(session, team, channels=[channel])
        event = await _create_event(session, cluster, team)
        await session.commit()

        await route_event(session, event, "firing")
        await session.commit()

        row = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert row.status == "pending"


# -- trailing-hour window boundary --------------------------------------------


async def test_auto_mode_ignores_rows_older_than_one_hour(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="auto", rate_limit_per_hour=2)
        await _create_rule(session, team, channels=[channel])
        now = datetime.now(UTC)
        # Both prior sends are outside the trailing-hour window.
        await _insert_raw_outbox_row(session, channel, team, created_at=now - timedelta(hours=2))
        await _insert_raw_outbox_row(session, channel, team, created_at=now - timedelta(minutes=61))
        event = await _create_event(session, cluster, team)
        await session.commit()

        await route_event(session, event, "firing")
        await session.commit()

        new_row = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event.id)
            )
        ).scalar_one()
        assert new_row.status == "pending"


async def test_auto_mode_counts_a_row_exactly_at_the_one_hour_edge(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="auto", rate_limit_per_hour=1)
        await _create_rule(session, team, channels=[channel])
        now = datetime.now(UTC)
        # created_at >= cutoff (cutoff == now - 1h) -- just inside the window.
        await _insert_raw_outbox_row(session, channel, team, created_at=now - timedelta(minutes=59, seconds=59))
        event = await _create_event(session, cluster, team)
        await session.commit()

        await route_event(session, event, "firing")
        await session.commit()

        new_row = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event.id)
            )
        ).scalar_one()
        assert new_row.status == "digested"


async def test_auto_mode_excludes_prior_digest_aggregate_rows_from_the_count(app) -> None:
    """An aggregate send must never itself count toward the rate that
    triggers more parking (see _channel_needs_parking's docstring) -- a
    channel with rate_limit=1 and one existing AGGREGATE row (is_digest=True)
    in the last hour still has an effective count of 0 for parking purposes.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="auto", rate_limit_per_hour=1)
        await _create_rule(session, team, channels=[channel])
        now = datetime.now(UTC)
        await _insert_raw_outbox_row(
            session, channel, team, created_at=now - timedelta(minutes=5), is_digest=True, trigger="digest"
        )
        event = await _create_event(session, cluster, team)
        await session.commit()

        await route_event(session, event, "firing")
        await session.commit()

        new_row = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event.id)
            )
        ).scalar_one()
        assert new_row.status == "pending"


# -- single-pending-digest_flush guarantee ------------------------------------


async def test_repeated_parking_schedules_only_one_pending_flush(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="always", digest_window_minutes=7)
        await _create_rule(session, team, channels=[channel])
        await session.commit()

        for _ in range(4):
            event = await _create_event(session, cluster, team)
            await route_event(session, event, "firing")
            await session.commit()

        parked = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.status == "digested")
            )
        ).scalars().all()
        assert len(parked) == 4

        pending = await _pending_digest_flush_actions(session, channel.id)
        assert len(pending) == 1
        assert pending[0].due_at < datetime.now(UTC) + timedelta(minutes=8)


# -- flush dispatch: 0 / 1 / N parked rows ------------------------------------


async def test_digest_flush_with_zero_parked_rows_is_a_noop_done(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = await _create_channel(session, team, digest_mode="always")
        action = ScheduledAction(
            kind="digest_flush", channel_id=channel.id, due_at=datetime.now(UTC), status="claimed"
        )
        session.add(action)
        await session.commit()

        await dispatch(action, session)

        assert action.status == "done"
        assert action.processed_at is not None
        assert (await session.execute(select(NotificationOutbox))).scalars().all() == []


async def test_digest_flush_with_one_parked_row_restores_pending(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="always")
        await _create_rule(session, team, channels=[channel])
        event = await _create_event(session, cluster, team)
        await session.commit()

        await route_event(session, event, "firing")
        await session.commit()
        parked_row = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert parked_row.status == "digested"

        [action] = await _pending_digest_flush_actions(session, channel.id)
        action.status = "claimed"
        await session.commit()

        await dispatch(action, session)

        assert action.status == "done"
        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 1
        await session.refresh(rows[0])
        assert rows[0].status == "pending"
        assert rows[0].digested_into_id is None
        assert rows[0].is_digest is False


async def test_digest_flush_with_multiple_parked_rows_creates_linked_aggregate(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="always")
        await _create_rule(session, team, channels=[channel])
        await session.commit()

        parked_ids = []
        for _ in range(3):
            event = await _create_event(session, cluster, team)
            await route_event(session, event, "firing")
            await session.commit()
            row = (
                await session.execute(
                    select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event.id)
                )
            ).scalar_one()
            assert row.status == "digested"
            parked_ids.append(row.id)

        [action] = await _pending_digest_flush_actions(session, channel.id)
        action.status = "claimed"
        await session.commit()

        await dispatch(action, session)
        await session.commit()

        assert action.status == "done"

        aggregate = (
            await session.execute(select(NotificationOutbox).where(NotificationOutbox.is_digest.is_(True)))
        ).scalar_one()
        assert aggregate.alert_event_id is None
        assert aggregate.routing_rule_id is None
        assert aggregate.channel_id == channel.id
        assert aggregate.team_id == channel.team_id
        assert aggregate.trigger == "digest"
        assert aggregate.status == "pending"
        assert aggregate.payload["count"] == 3
        assert len(aggregate.payload["notifications"]) == 3
        assert "window_started_at" in aggregate.payload

        parked_rows = (
            await session.execute(select(NotificationOutbox).where(NotificationOutbox.id.in_(parked_ids)))
        ).scalars().all()
        assert len(parked_rows) == 3
        for row in parked_rows:
            assert row.status == "digested"  # unchanged -- aggregate now owns delivery history
            assert row.digested_into_id == aggregate.id


async def test_flush_then_new_parking_schedules_a_fresh_flush(app) -> None:
    """After a flush settles 'done', it no longer blocks
    _ensure_digest_flush_scheduled's dedup lookup -- the very next parked
    row schedules a brand new pending digest_flush action.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="always")
        await _create_rule(session, team, channels=[channel])
        event1 = await _create_event(session, cluster, team)
        await session.commit()
        await route_event(session, event1, "firing")
        await session.commit()

        [first_action] = await _pending_digest_flush_actions(session, channel.id)
        first_action.status = "claimed"
        await session.commit()
        await dispatch(first_action, session)  # single parked row -> restored to pending
        await session.commit()
        assert first_action.status == "done"

        event2 = await _create_event(session, cluster, team)
        await route_event(session, event2, "firing")
        await session.commit()

        pending = await _pending_digest_flush_actions(session, channel.id)
        assert len(pending) == 1
        assert pending[0].id != first_action.id


async def test_digest_flush_channel_deleted_cancels(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = await _create_channel(session, team, digest_mode="always")
        channel.deleted_at = datetime.now(UTC)
        action = ScheduledAction(
            kind="digest_flush", channel_id=channel.id, due_at=datetime.now(UTC), status="claimed"
        )
        session.add(action)
        await session.commit()

        await dispatch(action, session)

        assert action.status == "cancelled"


# -- storm control applies regardless of trigger (escalation/renotify) -------


async def test_escalation_staging_respects_channel_parking(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        esc_channel = await _create_channel(session, team, name="esc", digest_mode="always")
        rule = await _create_rule(
            session,
            team,
            escalation_enabled=True,
            escalation_after_minutes=5,
            escalation_channels=[esc_channel],
        )
        event = await _create_event(session, cluster, team)
        action = ScheduledAction(
            kind="escalation",
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            due_at=datetime.now(UTC),
            status="claimed",
        )
        session.add(action)
        await session.commit()

        await dispatch(action, session)

        assert action.status == "done"
        row = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert row.trigger == "escalation"
        assert row.status == "digested"
        assert len(await _pending_digest_flush_actions(session, esc_channel.id)) == 1


async def test_renotify_staging_respects_channel_parking(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="always")
        rule = await _create_rule(session, team, channels=[channel], renotify_interval_minutes=15)
        event = await _create_event(session, cluster, team)
        action = ScheduledAction(
            kind="renotify",
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            due_at=datetime.now(UTC),
            status="claimed",
        )
        session.add(action)
        await session.commit()

        await dispatch(action, session)

        assert action.status == "done"
        row = (await session.execute(select(NotificationOutbox))).scalar_one()
        assert row.trigger.startswith("renotify:")
        assert row.status == "digested"


# -- UQ/NULL semantics: many aggregate rows can coexist per channel ----------


async def test_multiple_digest_aggregate_rows_coexist_for_same_channel(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = await _create_channel(session, team)
        agg1 = NotificationOutbox(
            alert_event_id=None,
            channel_id=channel.id,
            team_id=team.id,
            trigger="digest",
            payload={"notifications": [], "count": 0, "window_started_at": "x"},
            is_digest=True,
        )
        agg2 = NotificationOutbox(
            alert_event_id=None,
            channel_id=channel.id,
            team_id=team.id,
            trigger="digest",
            payload={"notifications": [], "count": 0, "window_started_at": "y"},
            is_digest=True,
        )
        session.add_all([agg1, agg2])
        await session.commit()  # must NOT raise IntegrityError

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 2


# -- send_batch: default per-item loop ----------------------------------------


async def test_send_batch_default_implementation_loops_over_send() -> None:
    calls: list[tuple[AlertNotification, RenderedMessage]] = []

    class FakeConfig(BaseModel):
        marker: str = "ok"

    class LoopOnlyChannel(NotificationChannel):
        type_name = "loop-only"
        display_name = "Loop Only"
        config_schema = FakeConfig

        async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
            calls.append((notification, msg))

    channel = LoopOnlyChannel(FakeConfig())
    n1 = AlertNotification.example().model_copy(update={"alertname": "A1"})
    n2 = AlertNotification.example().model_copy(update={"alertname": "A2"})
    msg1 = RenderedMessage(title="t1", body="b1")
    msg2 = RenderedMessage(title="t2", body="b2")

    await channel.send_batch([n1, n2], [msg1, msg2])

    assert calls == [(n1, msg1), (n2, msg2)]


async def test_send_batch_default_implementation_stops_on_first_failure() -> None:
    class FakeConfig(BaseModel):
        marker: str = "ok"

    class FailingChannel(NotificationChannel):
        type_name = "failing"
        display_name = "Failing"
        config_schema = FakeConfig

        async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
            raise ChannelDeliveryError("boom")

    channel = FailingChannel(FakeConfig())
    n = AlertNotification.example()
    msg = RenderedMessage(title="t", body="b")

    with pytest.raises(ChannelDeliveryError, match="boom"):
        await channel.send_batch([n], [msg])


# -- EmailChannel.send_batch: the built-in digest summary --------------------


def _parts(message) -> dict[str, str]:
    return {
        part.get_content_type(): part.get_payload(decode=True).decode("utf-8")
        for part in message.walk()
        if not part.is_multipart()
    }


async def test_email_channel_send_batch_builds_one_summary_message() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)
    notifications = [
        AlertNotification.example().model_copy(
            update={"alertname": f"Alert{i}", "severity": "critical" if i == 0 else "warning"}
        )
        for i in range(3)
    ]
    # msgs are per-item rendered messages the caller (deliver()) always
    # computes for the default-loop fallback -- EmailChannel's own
    # send_batch must ignore them entirely and build its own digest content.
    msgs = [RenderedMessage(title="IGNORED", body="IGNORED") for _ in notifications]

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send_batch(notifications, msgs)

    assert mock_send.await_count == 1
    message = mock_send.await_args.args[0]
    subject = message["Subject"]
    assert subject.startswith("[KAM]")
    assert "3건" in subject
    assert notifications[0].team_slug in subject
    assert "IGNORED" not in subject

    parts = _parts(message)
    assert "IGNORED" not in parts["text/plain"]
    for n in notifications:
        assert n.alertname in parts["text/plain"]
        assert n.alertname in parts["text/html"]


async def test_email_channel_send_batch_empty_list_sends_nothing() -> None:
    config = EmailConfig(recipients=["ops@example.org"])
    channel = EmailChannel(config)

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        await channel.send_batch([], [])

    mock_send.assert_not_awaited()


# -- deliver(): routing an is_digest row through send_batch -------------------


async def _create_digest_row(session, channel: Channel, team: Team, *, alertnames: list[str]) -> NotificationOutbox:
    items = [_example_payload(alertname=name) for name in alertnames]
    row = NotificationOutbox(
        alert_event_id=None,
        routing_rule_id=None,
        channel_id=channel.id,
        team_id=team.id,
        trigger="digest",
        payload={"notifications": items, "count": len(items), "window_started_at": "2026-01-01T00:00:00+00:00"},
        is_digest=True,
        status="pending",
    )
    session.add(row)
    await session.flush()
    return row


async def test_deliver_digest_row_success_calls_send_batch_with_every_item(app) -> None:
    fake_cls, sent, _fail_queue = _make_fake_channel_type()
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = Channel(
            team_id=team.id,
            name="c1",
            type=fake_cls.type_name,
            config_encrypted=encrypt_str('{"marker": "ok"}'),
        )
        session.add(channel)
        await session.flush()
        row = await _create_digest_row(session, channel, team, alertnames=["A1", "A2", "A3"])
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "delivered"
        assert row.delivered_at is not None
        assert {n.alertname for n in sent} == {"A1", "A2", "A3"}


async def test_deliver_digest_row_failure_schedules_retry(app) -> None:
    fake_cls, _sent, fail_queue = _make_fake_channel_type()
    fail_queue.append(ChannelDeliveryError("digest boom"))
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = Channel(
            team_id=team.id,
            name="c1",
            type=fake_cls.type_name,
            config_encrypted=encrypt_str('{"marker": "ok"}'),
        )
        session.add(channel)
        await session.flush()
        row = await _create_digest_row(session, channel, team, alertnames=["A1", "A2"])
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "pending"
        assert row.attempts == 1
        assert "digest boom" in row.last_error


async def test_deliver_digest_row_dead_after_max_attempts(app) -> None:
    fake_cls, _sent, fail_queue = _make_fake_channel_type()
    fail_queue.append(ChannelDeliveryError("digest boom"))
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = Channel(
            team_id=team.id,
            name="c1",
            type=fake_cls.type_name,
            config_encrypted=encrypt_str('{"marker": "ok"}'),
        )
        session.add(channel)
        await session.flush()
        row = await _create_digest_row(session, channel, team, alertnames=["A1"])
        row.attempts = MAX_ATTEMPTS - 1
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "dead"


async def test_deliver_digest_row_uses_channel_template_not_rule_template(app) -> None:
    """A digest aggregate row has no routing_rule_id -- resolve_template must
    be consulted with rule_template_id=None (channel's own template, or its
    type's/app default), never crash on a missing rule.
    """
    fake_cls, sent, _fail_queue = _make_fake_channel_type()
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        channel = Channel(
            team_id=team.id,
            name="c1",
            type=fake_cls.type_name,
            config_encrypted=encrypt_str('{"marker": "ok"}'),
        )
        session.add(channel)
        await session.flush()
        row = await _create_digest_row(session, channel, team, alertnames=["A1"])
        assert row.routing_rule_id is None
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "delivered"
        assert len(sent) == 1


# -- regression: an 'off' channel behaves exactly as before this phase ------


async def test_digest_off_channel_regression_unaffected_by_storm_control(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)  # defaults: off, no rate limit
        await _create_rule(session, team, channels=[channel])
        await session.commit()

        for _ in range(10):
            event = await _create_event(session, cluster, team)
            outcome = await route_event(session, event, "firing")
            assert outcome.routed is True
            assert outcome.channels_notified == 1
            await session.commit()

        rows = (await session.execute(select(NotificationOutbox))).scalars().all()
        assert len(rows) == 10
        assert all(r.status == "pending" and not r.is_digest for r in rows)
        assert (await session.execute(select(ScheduledAction))).scalars().all() == []


async def test_stage_outbox_row_dedup_unaffected_by_parking(app) -> None:
    """The pre-existing (alert_event_id, channel_id, trigger) UQ dedup skip
    still works the same whether or not the row would have been parked.
    """
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team, digest_mode="always")
        event = await _create_event(session, cluster, team)
        await session.commit()

        first = await stage_outbox_row(
            session,
            alert_event_id=event.id,
            routing_rule_id=None,
            channel=channel,
            team_id=team.id,
            trigger="firing",
            payload=_example_payload(),
        )
        await session.commit()
        second = await stage_outbox_row(
            session,
            alert_event_id=event.id,
            routing_rule_id=None,
            channel=channel,
            team_id=team.id,
            trigger="firing",
            payload=_example_payload(),
        )
        await session.commit()

        assert first is True
        assert second is False
        assert len((await session.execute(select(NotificationOutbox))).scalars().all()) == 1
