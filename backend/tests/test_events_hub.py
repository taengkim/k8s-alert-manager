"""Unit tests for `app.services.events_hub.Hub` (Phase 18): team-scoped
publish/subscribe fan-out and the bounded, drop-oldest queue. No app/DB
involved -- this is pure in-process pub/sub.
"""


import pytest

from app.services.events_hub import QUEUE_MAXSIZE, Hub, build_event


def _event(**overrides) -> dict:
    base = {
        "type": "alert_created",
        "event_id": 1,
        "team_id": None,
        "cluster": "local",
        "namespace": "kam-demo",
        "alertname": "TestAlert",
        "severity": "critical",
        "is_test": False,
        "ts": "2026-09-24T00:00:00+00:00",
    }
    base.update(overrides)
    return base


# -- team scope matrix --------------------------------------------------


async def test_own_team_subscriber_receives_own_team_event() -> None:
    hub = Hub()
    _sub_id, queue = hub.subscribe(team_ids={1}, is_admin=False)

    hub.publish(_event(team_id=1))

    seq, event = queue.get_nowait()
    assert seq == 1
    assert event["team_id"] == 1


async def test_other_team_subscriber_does_not_receive_event() -> None:
    hub = Hub()
    _sub_id, queue = hub.subscribe(team_ids={2}, is_admin=False)

    hub.publish(_event(team_id=1))

    assert queue.empty()


async def test_admin_receives_every_teams_event() -> None:
    hub = Hub()
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    hub.publish(_event(team_id=1))
    hub.publish(_event(team_id=2))

    assert queue.qsize() == 2


async def test_unassigned_event_reaches_only_admin() -> None:
    hub = Hub()
    _admin_id, admin_queue = hub.subscribe(team_ids=set(), is_admin=True)
    _member_id, member_queue = hub.subscribe(team_ids={1}, is_admin=False)

    hub.publish(_event(team_id=None))

    assert admin_queue.qsize() == 1
    assert member_queue.empty()


async def test_multiple_own_teams_all_covered() -> None:
    hub = Hub()
    _sub_id, queue = hub.subscribe(team_ids={1, 2}, is_admin=False)

    hub.publish(_event(team_id=1))
    hub.publish(_event(team_id=2))
    hub.publish(_event(team_id=3))

    assert queue.qsize() == 2


async def test_unsubscribe_stops_further_delivery() -> None:
    hub = Hub()
    sub_id, queue = hub.subscribe(team_ids={1}, is_admin=False)
    hub.unsubscribe(sub_id)

    hub.publish(_event(team_id=1))

    assert queue.empty()


async def test_unsubscribe_unknown_id_is_a_noop() -> None:
    hub = Hub()
    hub.unsubscribe(999)  # must not raise


# -- sequence numbering ---------------------------------------------------


async def test_sequence_is_monotonic_across_publishes() -> None:
    hub = Hub()
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    hub.publish(_event())
    hub.publish(_event())
    hub.publish(_event())

    seqs = [queue.get_nowait()[0] for _ in range(3)]
    assert seqs == [1, 2, 3]


async def test_sequence_is_shared_across_subscribers_of_the_same_publish() -> None:
    hub = Hub()
    _a_id, queue_a = hub.subscribe(team_ids=set(), is_admin=True)
    _b_id, queue_b = hub.subscribe(team_ids=set(), is_admin=True)

    hub.publish(_event())

    seq_a, _ = queue_a.get_nowait()
    seq_b, _ = queue_b.get_nowait()
    assert seq_a == seq_b == 1


# -- bounded queue / drop-oldest -------------------------------------------


async def test_full_queue_drops_oldest_and_keeps_maxsize() -> None:
    hub = Hub()
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    for i in range(QUEUE_MAXSIZE + 5):
        hub.publish(_event(event_id=i))

    assert queue.qsize() == QUEUE_MAXSIZE
    # The oldest 5 (event_id 0..4) were dropped to make room -- the surviving
    # queue starts at event_id=5 (seq=6, since sequence numbers are 1-based).
    first_seq, first_event = queue.get_nowait()
    assert first_event["event_id"] == 5
    assert first_seq == 6


async def test_full_queue_drop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    hub = Hub()
    hub.subscribe(team_ids=set(), is_admin=True)

    with caplog.at_level("WARNING"):
        for i in range(QUEUE_MAXSIZE + 1):
            hub.publish(_event(event_id=i))

    assert any("dropping oldest event" in record.message for record in caplog.records)


# -- build_event ------------------------------------------------------------


def test_build_event_shape() -> None:
    event = build_event(
        "alert_created",
        event_id=42,
        team_id=7,
        cluster="prod",
        namespace="kam-demo",
        alertname="KamAlwaysFiring",
        severity="critical",
        is_test=False,
    )
    assert event["type"] == "alert_created"
    assert event["event_id"] == 42
    assert event["team_id"] == 7
    assert event["cluster"] == "prod"
    assert event["namespace"] == "kam-demo"
    assert event["alertname"] == "KamAlwaysFiring"
    assert event["severity"] == "critical"
    assert event["is_test"] is False
    assert "ts" in event
