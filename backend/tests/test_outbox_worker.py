"""Tests for app/worker/outbox.py: claim/deliver/backoff/lease lifecycle.

Only the SQLite claim_batch branch is exercised here (the whole test suite
runs against an in-memory SQLite db -- see tests/conftest.py); the
Postgres `FOR UPDATE SKIP LOCKED` branch is straightforward, well-trodden
SQL and isn't covered by an automated test in this repo, since that would
require a live Postgres instance.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

import app.db as db_module
from app.channels.base import (
    AlertNotification,
    ChannelDeliveryError,
    NotificationChannel,
    RenderedMessage,
)
from app.channels.email import EmailConfig
from app.channels.registry import ChannelRegistry
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.team import Team
from app.security import encrypt_str
from app.worker.outbox import (
    claim_batch,
    deliver,
    recover_stale_leases,
    run_tick,
)


def _make_fake_channel_type(type_name: str = "fake"):
    """A fresh, isolated fake channel class + its shared mutable state
    (`sent` notifications, a FIFO of exceptions to raise before succeeding).
    A factory (not a module-level class) so state never leaks across tests.
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


async def _create_team(session, slug: str = "platform") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(session, name: str = "rt-cluster") -> Cluster:
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
    session, team: Team, *, type_: str = "email", name: str = "c1", enabled: bool = True
) -> Channel:
    if type_ == "email":
        config_json = EmailConfig(recipients=["ops@example.org"]).model_dump_json()
    else:
        config_json = '{"marker": "ok"}'
    channel = Channel(
        team_id=team.id,
        name=name,
        type=type_,
        config_encrypted=encrypt_str(config_json),
        enabled=enabled,
    )
    session.add(channel)
    await session.flush()
    return channel


async def _create_event(session, cluster: Cluster, team: Team, *, fingerprint: str) -> AlertEvent:
    event = AlertEvent(
        cluster_id=cluster.id,
        cluster_name=cluster.name,
        fingerprint=fingerprint,
        status="firing",
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


def _example_payload() -> dict:
    return AlertNotification.example().model_dump(mode="json")


async def _create_outbox_row(
    session,
    team: Team,
    channel: Channel,
    event: AlertEvent,
    *,
    trigger: str = "firing",
    next_attempt_at: datetime | None = None,
    status: str = "pending",
    attempts: int = 0,
) -> NotificationOutbox:
    row = NotificationOutbox(
        alert_event_id=event.id,
        channel_id=channel.id,
        team_id=team.id,
        trigger=trigger,
        payload=_example_payload(),
        status=status,
        attempts=attempts,
        next_attempt_at=next_attempt_at or datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def _setup(session, *, channel_type: str = "email", channel_enabled: bool = True):
    team = await _create_team(session)
    cluster = await _create_cluster(session)
    channel = await _create_channel(session, team, type_=channel_type, enabled=channel_enabled)
    return team, cluster, channel


async def test_claim_batch_claims_due_pending_rows_and_marks_in_progress(app) -> None:
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session)
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        claimed = await claim_batch(session, "worker-1")

        assert [r.id for r in claimed] == [row.id]
        assert claimed[0].status == "in_progress"
        assert claimed[0].locked_by == "worker-1"


async def test_claim_batch_skips_rows_not_yet_due(app) -> None:
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session)
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        await _create_outbox_row(
            session, team, channel, event, next_attempt_at=datetime.now(UTC) + timedelta(hours=1)
        )
        await session.commit()

        claimed = await claim_batch(session, "worker-1")
        assert claimed == []


async def test_claim_batch_respects_limit(app) -> None:
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session)
        for i in range(5):
            event = await _create_event(session, cluster, team, fingerprint=f"fp-{i}")
            await _create_outbox_row(session, team, channel, event)
        await session.commit()

        claimed = await claim_batch(session, "worker-1", limit=2)
        assert len(claimed) == 2


async def test_deliver_success_marks_delivered(app) -> None:
    fake_cls, sent, _fail_queue = _make_fake_channel_type()
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "delivered"
        assert row.delivered_at is not None
        assert len(sent) == 1


async def test_deliver_failure_schedules_backoff_and_increments_attempts(app) -> None:
    fake_cls, _sent, fail_queue = _make_fake_channel_type()
    fail_queue.append(ChannelDeliveryError("simulated"))
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        before = datetime.now(UTC)
        await deliver(row, registry, session)

        assert row.status == "pending"
        assert row.attempts == 1
        assert "simulated" in row.last_error
        # backoff = min(30 * 2**1, 1800) = 60s, plus 0-15s jitter.
        assert row.next_attempt_at >= before + timedelta(seconds=59)
        assert row.next_attempt_at <= before + timedelta(seconds=76)


async def test_deliver_marks_dead_after_max_attempts(app) -> None:
    fake_cls, _sent, fail_queue = _make_fake_channel_type()
    fail_queue.append(ChannelDeliveryError("simulated"))
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event, attempts=7)
        await session.commit()

        await deliver(row, registry, session)

        assert row.attempts == 8
        assert row.status == "dead"


async def test_deliver_disabled_channel_marks_dead(app) -> None:
    registry = _registry_with()
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_enabled=False)
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "dead"
        assert "disabled" in row.last_error


async def test_deliver_unknown_channel_type_marks_dead(app) -> None:
    """A channel whose type is no longer registered (e.g. an uninstalled
    plugin) can't be delivered through -- dead, not an infinite retry."""
    registry = _registry_with()  # only builtins -- "ghost-plugin" isn't one
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type="ghost-plugin")
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "dead"
        assert "ghost-plugin" in row.last_error


async def test_recover_stale_leases_resets_expired_in_progress_rows(app) -> None:
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session)
        event1 = await _create_event(session, cluster, team, fingerprint="fp-1")
        event2 = await _create_event(session, cluster, team, fingerprint="fp-2")
        stale = await _create_outbox_row(session, team, channel, event1, status="in_progress")
        stale.locked_by = "dead-worker"
        stale.locked_at = datetime.now(UTC) - timedelta(minutes=10)
        fresh = await _create_outbox_row(session, team, channel, event2, status="in_progress")
        fresh.locked_by = "live-worker"
        fresh.locked_at = datetime.now(UTC)
        await session.commit()

        recovered = await recover_stale_leases(session, lease_timeout=timedelta(minutes=5))

        assert recovered == 1
        await session.refresh(stale)
        await session.refresh(fresh)
        assert stale.status == "pending"
        assert stale.locked_by is None
        assert fresh.status == "in_progress"  # untouched -- lease still fresh


async def test_run_tick_delivers_then_retries_across_two_ticks(app) -> None:
    """A near end-to-end run of the worker loop's per-tick unit
    (claim_batch + deliver together): with a single queued failure shared
    across both rows' channel, exactly one of the two fails-and-retries
    while the other is delivered outright on the first tick; the failed one
    only succeeds once its backoff has passed.
    """
    fake_cls, sent, fail_queue = _make_fake_channel_type()
    fail_queue.append(ChannelDeliveryError("first attempt fails"))
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)
        event1 = await _create_event(session, cluster, team, fingerprint="fp-1")
        event2 = await _create_event(session, cluster, team, fingerprint="fp-2")
        row1 = await _create_outbox_row(session, team, channel, event1)
        row2 = await _create_outbox_row(session, team, channel, event2)
        await session.commit()
        row_ids = [row1.id, row2.id]

    claimed_count = await run_tick(db_module.async_session_factory, registry, "worker-1")
    assert claimed_count == 2

    async with db_module.async_session_factory() as session:
        rows = [await session.get(NotificationOutbox, rid) for rid in row_ids]
        statuses = {r.status for r in rows}
        assert statuses == {"delivered", "pending"}
        failed_row = next(r for r in rows if r.status == "pending")
        assert failed_row.attempts == 1
        # Force the retry due now instead of sleeping out the real backoff.
        failed_row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        failed_id = failed_row.id
        await session.commit()

    claimed_count_2 = await run_tick(db_module.async_session_factory, registry, "worker-1")
    assert claimed_count_2 == 1

    async with db_module.async_session_factory() as session:
        failed_row = await session.get(NotificationOutbox, failed_id)
        assert failed_row.status == "delivered"

    assert len(sent) == 2


async def test_claim_batch_dedup_uq_still_holds_after_worker_touches_rows(app) -> None:
    """Sanity check that claiming/delivering rows doesn't disturb the
    (alert_event_id, channel_id, trigger) UQ dedup guarantee route_event
    relies on -- a second identical outbox insert must still be rejected
    even after the first row has already been delivered.
    """
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session)
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        await _create_outbox_row(session, team, channel, event, status="delivered")
        await session.commit()

        dup = NotificationOutbox(
            alert_event_id=event.id,
            channel_id=channel.id,
            team_id=team.id,
            trigger="firing",
            payload=_example_payload(),
        )
        session.add(dup)
        try:
            await session.flush()
        except IntegrityError:
            pass
        else:
            raise AssertionError("expected UQ violation")


async def test_deliver_invalid_stored_config_marks_dead_without_incrementing_attempts(
    app,
) -> None:
    """A stored config that fails its own channel type's schema (e.g. it
    predates a schema change, or was corrupted) can never succeed no matter
    how many times it's retried -- straight to dead, attempts untouched.
    """
    registry = _registry_with()  # builtin email is enough here
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session)
        # EmailConfig.recipients requires min_length=1.
        channel.config_encrypted = encrypt_str('{"recipients": []}')
        await session.flush()
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "dead"
        assert row.attempts == 0
        assert "invalid channel config" in row.last_error


async def test_deliver_corrupted_ciphertext_retries_instead_of_fast_dead(app) -> None:
    """Undecryptable ciphertext usually means the *key* is wrong (a
    rotated/misconfigured KAM_SECRET_KEY on this process), not corrupted
    data -- that's an operator-fixable misconfiguration, so it must go
    through the normal backoff-and-retry path (giving ~40 minutes to fix
    the key) rather than fast-deading and wiping out the whole pending
    queue the moment a key issue hits.
    """
    registry = _registry_with()
    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session)
        channel.config_encrypted = "not-valid-fernet-ciphertext"
        await session.flush()
        event = await _create_event(session, cluster, team, fingerprint="fp-1")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "pending"
        assert row.attempts == 1
        assert "InvalidToken" in row.last_error


async def test_run_tick_one_bad_row_does_not_block_the_rest_of_the_batch(app) -> None:
    """A failure that escapes deliver() entirely (not one of the failure
    modes it catches internally) must not stop run_tick from delivering
    the other rows in the same batch -- regardless of claim order. The
    poison row is created (and thus claimed) FIRST here specifically to
    prove that: each row gets its own delivery session, so the poisoned
    row's session breaking can't expire or otherwise disturb the good
    row's ORM state in a shared session.

    `registry.get(channel.type)` is the injection point -- it's the one
    lookup in deliver() genuinely outside any try/except, so raising there
    is the realistic way to simulate something breaking deliver() itself
    rather than a channel-level failure it already handles (every failure
    mode reachable from decrypting/validating a config or calling send()
    is deliberately caught internally, by design).
    """
    good_cls, sent, _good_fail_queue = _make_fake_channel_type("good-fake")
    bad_cls, _bad_sent, _bad_fail_queue = _make_fake_channel_type("bad-fake")
    registry = _registry_with(good_cls, bad_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel_good = await _setup(session, channel_type=good_cls.type_name)
        channel_bad = await _create_channel(session, team, type_=bad_cls.type_name, name="bad")
        event_bad = await _create_event(session, cluster, team, fingerprint="fp-bad")
        event_good = await _create_event(session, cluster, team, fingerprint="fp-good")
        # Explicitly ordered so the bad row is claimed (and thus attempted)
        # before the good one -- claim_batch orders by next_attempt_at.
        bad_row = await _create_outbox_row(
            session,
            team,
            channel_bad,
            event_bad,
            next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
        )
        good_row = await _create_outbox_row(session, team, channel_good, event_good)
        await session.commit()
        good_id, bad_id = good_row.id, bad_row.id

    real_get = registry.get

    def flaky_get(type_name: str):
        if type_name == bad_cls.type_name:
            raise RuntimeError("simulated unexpected failure")
        return real_get(type_name)

    with patch.object(registry, "get", side_effect=flaky_get):
        claimed = await run_tick(db_module.async_session_factory, registry, "worker-1")

    assert claimed == 2

    async with db_module.async_session_factory() as session:
        good_row = await session.get(NotificationOutbox, good_id)
        bad_row = await session.get(NotificationOutbox, bad_id)
        assert good_row.status == "delivered"
        # run_tick's per-row session isolated the escaped exception,
        # leaving the bad row claimed (in_progress) for the next
        # lease-recovery pass rather than stuck mid-transaction or lost.
        assert bad_row.status == "in_progress"

    assert len(sent) == 1

    assert len(sent) == 1


# -- Phase 13: template resolution + rendering at delivery time ----------------


async def _make_recording_channel_type(type_name: str):
    """Like `_make_fake_channel_type`, but also records the `RenderedMessage`
    each `send()` call received -- these template-resolution/fallback tests
    need to assert on the rendered title, not just that delivery succeeded.
    """
    sent_messages: list[RenderedMessage] = []

    class FakeConfig(BaseModel):
        marker: str = "ok"

    class RecordingChannel(NotificationChannel):
        display_name = "Recording"
        config_schema = FakeConfig

        async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
            sent_messages.append(msg)

    RecordingChannel.type_name = type_name
    return RecordingChannel, sent_messages


async def test_deliver_resolves_rule_template_over_channel_template(app) -> None:
    """Priority order: a routing rule's own template_id wins over the
    channel's, which wins over the channel type's default_templates, which
    wins over the app-wide default -- see
    app.services.templating.resolve_template.
    """
    from app.models.routing import RoutingRule
    from app.models.template import MessageTemplate

    fake_cls, sent_messages = await _make_recording_channel_type("template-priority-fake")
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)

        rule_template = MessageTemplate(
            team_id=team.id,
            name="rule-tpl",
            title_template="RULE-WINS: {{ alertname }}",
            body_template="rule body",
        )
        channel_template = MessageTemplate(
            team_id=team.id,
            name="channel-tpl",
            title_template="CHANNEL: {{ alertname }}",
            body_template="channel body",
        )
        session.add_all([rule_template, channel_template])
        await session.flush()

        channel.template_id = channel_template.id
        rule = RoutingRule(
            team_id=team.id,
            name="rule-with-template",
            action="notify",
            template_id=rule_template.id,
            channels=[channel],
        )
        session.add(rule)
        await session.flush()

        event = await _create_event(session, cluster, team, fingerprint="fp-priority")
        row = NotificationOutbox(
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            channel_id=channel.id,
            team_id=team.id,
            trigger="firing",
            payload=AlertNotification.example()
            .model_copy(update={"alertname": "RuleTemplateAlert"})
            .model_dump(mode="json"),
        )
        session.add(row)
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "delivered"

    assert len(sent_messages) == 1
    assert sent_messages[0].title == "RULE-WINS: RuleTemplateAlert"


async def test_deliver_falls_back_to_channel_template_when_rule_has_none(app) -> None:
    from app.models.routing import RoutingRule
    from app.models.template import MessageTemplate

    fake_cls, sent_messages = await _make_recording_channel_type("template-channel-fake")
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)

        channel_template = MessageTemplate(
            team_id=team.id,
            name="channel-tpl-2",
            title_template="CHANNEL-WINS: {{ alertname }}",
            body_template="channel body",
        )
        session.add(channel_template)
        await session.flush()
        channel.template_id = channel_template.id

        # No template_id on this rule -- resolve_template must fall through
        # to the channel's.
        rule = RoutingRule(team_id=team.id, name="rule-no-template", action="notify", channels=[channel])
        session.add(rule)
        await session.flush()

        event = await _create_event(session, cluster, team, fingerprint="fp-channel-fallback")
        row = NotificationOutbox(
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            channel_id=channel.id,
            team_id=team.id,
            trigger="firing",
            payload=AlertNotification.example()
            .model_copy(update={"alertname": "ChannelTemplateAlert"})
            .model_dump(mode="json"),
        )
        session.add(row)
        await session.commit()

        await deliver(row, registry, session)
        assert row.status == "delivered"

    assert sent_messages[0].title == "CHANNEL-WINS: ChannelTemplateAlert"


async def test_deliver_broken_template_falls_back_and_records_last_error(app) -> None:
    """A team's own template with a syntax error must never block delivery
    -- render() falls back to the app default, delivery still succeeds, and
    last_error records the fallback so the team can notice their template
    is broken.
    """
    from app.models.routing import RoutingRule
    from app.models.template import MessageTemplate

    fake_cls, sent_messages = await _make_recording_channel_type("template-broken-fake")
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)

        broken_template = MessageTemplate(
            team_id=team.id,
            name="broken-tpl",
            title_template="{% if unterminated",
            body_template="ok",
        )
        session.add(broken_template)
        await session.flush()
        channel.template_id = broken_template.id

        rule = RoutingRule(team_id=team.id, name="rule-broken", action="notify", channels=[channel])
        session.add(rule)
        await session.flush()

        event = await _create_event(session, cluster, team, fingerprint="fp-broken")
        row = NotificationOutbox(
            alert_event_id=event.id,
            routing_rule_id=rule.id,
            channel_id=channel.id,
            team_id=team.id,
            trigger="firing",
            payload=AlertNotification.example()
            .model_copy(update={"alertname": "BrokenTemplateAlert"})
            .model_dump(mode="json"),
        )
        session.add(row)
        await session.commit()

        await deliver(row, registry, session)

        assert row.status == "delivered"
        assert "template render failed" in row.last_error
        assert "fallback used" in row.last_error

    assert "BrokenTemplateAlert" in sent_messages[0].title  # app default template rendered instead


async def test_deliver_with_no_template_anywhere_uses_app_default(app) -> None:
    fake_cls, sent_messages = await _make_recording_channel_type("template-none-fake")
    registry = _registry_with(fake_cls)

    async with db_module.async_session_factory() as session:
        team, cluster, channel = await _setup(session, channel_type=fake_cls.type_name)
        event = await _create_event(session, cluster, team, fingerprint="fp-no-template")
        row = await _create_outbox_row(session, team, channel, event)
        await session.commit()

        await deliver(row, registry, session)
        assert row.status == "delivered"

    assert sent_messages[0].title  # app default rendered something non-empty
    assert row.last_error is None
