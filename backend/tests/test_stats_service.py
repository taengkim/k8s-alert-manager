"""Tests for app.services.stats: per-function accuracy against seeded
alert_events/notification_outbox rows, the dialect-branched volume() bucket
helper (SQLite path -- the only dialect this test suite runs against), and
the documented edge cases (0 acked -> mtta None, an empty period, is_test
exclusion, cluster-id scoping).
"""

from datetime import UTC, datetime, timedelta

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.team import Team
from app.security import encrypt_str
from app.services import stats

NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)


async def _create_team(session, slug: str = "platform") -> Team:
    team = Team(slug=slug, name=slug.title())
    session.add(team)
    await session.flush()
    return team


async def _create_cluster(session, name: str = "stats-cluster") -> Cluster:
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


async def _create_channel(session, team: Team, name: str = "c1") -> Channel:
    channel = Channel(
        team_id=team.id,
        name=name,
        type="email",
        config_encrypted=encrypt_str('{"recipients": ["ops@example.org"]}'),
    )
    session.add(channel)
    await session.flush()
    return channel


def _make_event(
    cluster: Cluster,
    team: Team | None,
    *,
    fingerprint: str,
    alertname: str = "HighCpu",
    severity: str | None = "critical",
    namespace: str | None = "kam-demo",
    status: str = "firing",
    starts_at: datetime,
    ends_at: datetime | None = None,
    acknowledged_at: datetime | None = None,
    receive_count: int = 1,
    is_test: bool = False,
) -> AlertEvent:
    return AlertEvent(
        cluster_id=cluster.id,
        cluster_name=cluster.name,
        fingerprint=fingerprint,
        status=status,
        alertname=alertname,
        severity=severity,
        namespace=namespace,
        labels={"alertname": alertname},
        annotations={},
        team_id=team.id if team is not None else None,
        starts_at=starts_at,
        ends_at=ends_at,
        first_received_at=starts_at,
        last_received_at=starts_at,
        acknowledged_at=acknowledged_at,
        receive_count=receive_count,
        is_test=is_test,
    )


def _make_outbox(
    event: AlertEvent,
    channel: Channel,
    team: Team,
    *,
    status: str = "delivered",
    attempts: int = 0,
    trigger: str = "firing",
    created_at: datetime,
) -> NotificationOutbox:
    return NotificationOutbox(
        alert_event_id=event.id,
        channel_id=channel.id,
        team_id=team.id,
        trigger=trigger,
        payload={"alertname": event.alertname},
        status=status,
        attempts=attempts,
        created_at=created_at,
    )


RANGE_FROM = NOW - timedelta(days=2)
RANGE_TO = NOW + timedelta(days=1)


# -- top_alerts ---------------------------------------------------------


async def test_top_alerts_counts_and_orders_desc(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        session.add_all(
            [
                _make_event(cluster, team, fingerprint="a1", alertname="Loud", starts_at=NOW, receive_count=3),
                _make_event(cluster, team, fingerprint="a2", alertname="Loud", starts_at=NOW, receive_count=2),
                _make_event(cluster, team, fingerprint="a3", alertname="Quiet", starts_at=NOW, receive_count=1),
            ]
        )
        await session.commit()

        rows = await stats.top_alerts(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO
        )
        assert rows[0] == {"alertname": "Loud", "count": 2, "receive_total": 5}
        assert rows[1] == {"alertname": "Quiet", "count": 1, "receive_total": 1}


async def test_top_alerts_excludes_test_events(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        session.add_all(
            [
                _make_event(cluster, team, fingerprint="a1", alertname="Real", starts_at=NOW),
                _make_event(cluster, team, fingerprint="a2", alertname="Synthetic", starts_at=NOW, is_test=True),
            ]
        )
        await session.commit()

        rows = await stats.top_alerts(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO
        )
        assert [r["alertname"] for r in rows] == ["Real"]


async def test_top_alerts_respects_cluster_filter(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster_a = await _create_cluster(session, "cluster-a")
        cluster_b = await _create_cluster(session, "cluster-b")
        session.add_all(
            [
                _make_event(cluster_a, team, fingerprint="a1", alertname="InA", starts_at=NOW),
                _make_event(cluster_b, team, fingerprint="b1", alertname="InB", starts_at=NOW),
            ]
        )
        await session.commit()

        rows = await stats.top_alerts(
            session,
            team_id=None,
            cluster_ids=[cluster_a.id],
            from_ts=RANGE_FROM,
            to_ts=RANGE_TO,
        )
        assert [r["alertname"] for r in rows] == ["InA"]


# -- volume ---------------------------------------------------------------


async def test_volume_buckets_by_hour_and_day(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        base = NOW.replace(minute=0, second=0, microsecond=0)
        session.add_all(
            [
                _make_event(cluster, team, fingerprint="h1", starts_at=base),
                _make_event(cluster, team, fingerprint="h2", starts_at=base + timedelta(minutes=30)),
                _make_event(cluster, team, fingerprint="h3", starts_at=base + timedelta(hours=1)),
            ]
        )
        await session.commit()

        hourly = await stats.volume(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO, bucket="hour"
        )
        assert len(hourly) == 2
        for row in hourly:
            assert isinstance(row["bucket_start"], datetime)
            assert row["bucket_start"].tzinfo is not None
        assert sum(row["firing_count"] for row in hourly) == 3

        daily = await stats.volume(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO, bucket="day"
        )
        assert len(daily) == 1
        assert daily[0]["firing_count"] == 3
        assert daily[0]["bucket_start"].hour == 0


async def test_volume_empty_period_returns_empty_list(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        session.add(_make_event(cluster, team, fingerprint="a1", starts_at=NOW))
        await session.commit()

        rows = await stats.volume(
            session, team_id=None, cluster_ids=None, from_ts=NOW, to_ts=NOW, bucket="day"
        )
        assert rows == []


# -- breakdown --------------------------------------------------------------


async def test_breakdown_by_severity_namespace_cluster(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        session.add_all(
            [
                _make_event(cluster, team, fingerprint="a1", severity="critical", namespace="ns-a", starts_at=NOW),
                _make_event(cluster, team, fingerprint="a2", severity="critical", namespace="ns-b", starts_at=NOW),
                _make_event(cluster, team, fingerprint="a3", severity=None, namespace=None, starts_at=NOW),
            ]
        )
        await session.commit()

        by_severity = await stats.breakdown(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO, by="severity"
        )
        assert {"key": "critical", "count": 2} in by_severity
        assert {"key": "none", "count": 1} in by_severity

        by_namespace = await stats.breakdown(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO, by="namespace"
        )
        assert {"key": "none", "count": 1} in by_namespace

        by_cluster = await stats.breakdown(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO, by="cluster"
        )
        assert by_cluster == [{"key": cluster.name, "count": 3}]


async def test_breakdown_by_team_uses_slug_and_none_for_unassigned(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session, "platform")
        cluster = await _create_cluster(session)
        session.add_all(
            [
                _make_event(cluster, team, fingerprint="a1", starts_at=NOW),
                _make_event(cluster, None, fingerprint="a2", starts_at=NOW),
            ]
        )
        await session.commit()

        rows = await stats.breakdown(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO, by="team"
        )
        assert {"key": "platform", "count": 1} in rows
        assert {"key": "none", "count": 1} in rows


async def test_breakdown_by_team_scoped_to_own_team_only(app) -> None:
    """A non-admin's forced team_id filter collapses a by=team breakdown to
    just their own team -- no special-casing in the service, the WHERE
    clause already restricts it (see breakdown()'s docstring).
    """
    async with db_module.async_session_factory() as session:
        team_a = await _create_team(session, "platform")
        team_b = await _create_team(session, "payments")
        cluster = await _create_cluster(session)
        session.add_all(
            [
                _make_event(cluster, team_a, fingerprint="a1", starts_at=NOW),
                _make_event(cluster, team_b, fingerprint="b1", starts_at=NOW),
            ]
        )
        await session.commit()

        rows = await stats.breakdown(
            session, team_id=team_a.id, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO, by="team"
        )
        assert rows == [{"key": "platform", "count": 1}]


# -- response_times -----------------------------------------------------


async def test_response_times_computes_mtta_and_mttr(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        ev = _make_event(
            cluster,
            team,
            fingerprint="a1",
            status="resolved",
            starts_at=NOW,
            ends_at=NOW + timedelta(minutes=10),
            acknowledged_at=NOW + timedelta(minutes=5),
        )
        session.add(ev)
        await session.commit()

        result = await stats.response_times(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO
        )
        assert result["mtta_seconds"] == 300.0
        assert result["mttr_seconds"] == 600.0
        assert result["acked_count"] == 1
        assert result["resolved_count"] == 1


async def test_response_times_zero_acked_is_none_not_zero(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        session.add(_make_event(cluster, team, fingerprint="a1", starts_at=NOW))
        await session.commit()

        result = await stats.response_times(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO
        )
        assert result["mtta_seconds"] is None
        assert result["mttr_seconds"] is None
        assert result["acked_count"] == 0
        assert result["resolved_count"] == 0


# -- summary --------------------------------------------------------------


async def test_summary_counts(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        channel = await _create_channel(session, team)

        firing = _make_event(cluster, team, fingerprint="f1", status="firing", starts_at=NOW)
        resolved = _make_event(cluster, team, fingerprint="f2", status="resolved", starts_at=NOW, ends_at=NOW)
        test_event = _make_event(
            cluster, team, fingerprint="f3", status="firing", starts_at=NOW, is_test=True
        )
        session.add_all([firing, resolved, test_event])
        await session.flush()

        session.add_all(
            [
                _make_outbox(firing, channel, team, status="delivered", created_at=NOW),
                _make_outbox(resolved, channel, team, status="dead", created_at=NOW),
                _make_outbox(
                    resolved, channel, team, status="pending", attempts=2,
                    trigger="resolved", created_at=NOW,
                ),
                # A currently-untouched pending row (attempts=0) is neither
                # failed nor dead -- not counted.
                _make_outbox(
                    firing, channel, team, status="pending", attempts=0,
                    trigger="escalation", created_at=NOW,
                ),
            ]
        )
        await session.commit()

        result = await stats.summary(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO
        )
        # firing_now: real (non-test) firing events only -- `firing` counts,
        # `test_event` (is_test=True) does not.
        assert result["firing_now"] == 1
        assert result["events_in_range"] == 2
        assert result["delivered_in_range"] == 1
        assert result["failed_or_dead_in_range"] == 2


async def test_summary_firing_now_independent_of_range(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        old_firing = _make_event(
            cluster, team, fingerprint="old", status="firing", starts_at=NOW - timedelta(days=30)
        )
        session.add(old_firing)
        await session.commit()

        # A range that excludes the event's starts_at entirely.
        result = await stats.summary(
            session, team_id=None, cluster_ids=None, from_ts=RANGE_FROM, to_ts=RANGE_TO
        )
        assert result["firing_now"] == 1
        assert result["events_in_range"] == 0


async def test_summary_empty_period(app) -> None:
    async with db_module.async_session_factory() as session:
        team = await _create_team(session)
        cluster = await _create_cluster(session)
        session.add(_make_event(cluster, team, fingerprint="a1", starts_at=NOW))
        await session.commit()

        result = await stats.summary(
            session, team_id=None, cluster_ids=None, from_ts=NOW, to_ts=NOW
        )
        assert result["events_in_range"] == 0
        assert result["delivered_in_range"] == 0
        assert result["failed_or_dead_in_range"] == 0
