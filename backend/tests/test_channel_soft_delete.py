"""Channel soft-delete behavior (app/api/channels.py, app/api/routes.py,
app/services/routing.py, app/worker/outbox.py): DELETE sets `deleted_at`
rather than removing the row, so delivery history survives; every other
surface (lists, pickers, routing, staged notifications, the worker) treats
a soft-deleted channel as gone.
"""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.channels.registry import ChannelRegistry
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.team import Team
from app.security import encrypt_str
from app.worker.outbox import deliver
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _create_team(slug: str) -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _create_cluster(name: str = "rt-cluster") -> int:
    async with db_module.async_session_factory() as session:
        cluster = Cluster(
            name=name,
            display_name=name,
            prometheus_url="http://prom",
            alertmanager_url="http://am",
            webhook_token_hash=f"hash-{name}",
        )
        session.add(cluster)
        await session.commit()
        await session.refresh(cluster)
        return cluster.id


async def _create_event(team_id: int, cluster_id: int, fingerprint: str) -> int:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        event = AlertEvent(
            cluster_id=cluster_id,
            cluster_name=cluster.name,
            fingerprint=fingerprint,
            status="firing",
            alertname="HighCpu",
            severity="critical",
            namespace="kam-demo",
            labels={"alertname": "HighCpu"},
            annotations={},
            team_id=team_id,
            starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event.id


async def test_delete_channel_with_delivery_history_returns_204(client: AsyncClient) -> None:
    team_id = await _create_team("t-soft-hist")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "hist-ch", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    channel_id = create_resp.json()["id"]

    cluster_id = await _create_cluster()
    event_id = await _create_event(team_id, cluster_id, "fp-hist")
    async with db_module.async_session_factory() as session:
        session.add(
            NotificationOutbox(
                alert_event_id=event_id,
                channel_id=channel_id,
                team_id=team_id,
                trigger="firing",
                payload={"alertname": "HighCpu"},
                status="delivered",
            )
        )
        await session.commit()

    resp = await client.delete(f"/api/v1/channels/{channel_id}")
    assert resp.status_code == 204

    history_resp = await client.get(f"/api/v1/alerts/history/{event_id}/notifications")
    assert history_resp.status_code == 200
    [row] = history_resp.json()
    assert row["channel_name"] == "hist-ch"


async def test_deleted_channel_absent_from_list_and_get_404(client: AsyncClient) -> None:
    team_id = await _create_team("t-soft-list")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "will-vanish", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    channel_id = create_resp.json()["id"]

    await client.delete(f"/api/v1/channels/{channel_id}")

    list_resp = await client.get(f"/api/v1/teams/{team_id}/channels")
    assert channel_id not in [c["id"] for c in list_resp.json()]

    patch_resp = await client.patch(f"/api/v1/channels/{channel_id}", json={"name": "x"})
    assert patch_resp.status_code == 404

    test_resp = await client.post(f"/api/v1/channels/{channel_id}/test")
    assert test_resp.status_code == 404


async def test_channel_name_reusable_after_delete(client: AsyncClient) -> None:
    team_id = await _create_team("t-soft-reuse")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    first = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "reused-name", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    assert first.status_code == 201

    await client.delete(f"/api/v1/channels/{first.json()['id']}")

    second = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "reused-name", "type": "email", "config": {"recipients": ["b@example.org"]}},
    )
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]


async def test_route_create_with_deleted_channel_id_is_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-soft-route")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "soon-gone", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    channel_id = create_resp.json()["id"]
    await client.delete(f"/api/v1/channels/{channel_id}")

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json={
            "name": "r1",
            "action": "notify",
            "channel_ids": [channel_id],
            "matchers": [],
        },
    )
    assert resp.status_code == 422


async def test_route_event_skips_soft_deleted_channel(app) -> None:
    from app.services.routing import route_event

    team_id = await _create_team("t-soft-route-event")
    cluster_id = await _create_cluster()
    async with db_module.async_session_factory() as session:
        channel = Channel(
            team_id=team_id,
            name="c1",
            type="email",
            config_encrypted=encrypt_str('{"recipients": ["a@example.org"]}'),
            deleted_at=datetime.now(UTC),
        )
        session.add(channel)
        await session.flush()
        rule = RoutingRule(team_id=team_id, name="r1", action="notify", channels=[channel])
        session.add(rule)
        await session.flush()

        cluster = await session.get(Cluster, cluster_id)
        event = AlertEvent(
            cluster_id=cluster_id,
            cluster_name=cluster.name,
            fingerprint="fp-skip",
            status="firing",
            alertname="HighCpu",
            severity="critical",
            namespace="kam-demo",
            labels={"alertname": "HighCpu"},
            annotations={},
            team_id=team_id,
            starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(event)
        await session.flush()

        outcome = await route_event(session, event, "firing")
        await session.commit()

        assert outcome.routed is False
        assert outcome.reason == "no_match"
        rows = (
            await session.execute(select(NotificationOutbox))
        ).scalars().all()
        assert rows == []


async def test_worker_marks_dead_when_channel_soft_deleted_after_staging(app) -> None:
    team_id = await _create_team("t-soft-worker")
    cluster_id = await _create_cluster()
    event_id = await _create_event(team_id, cluster_id, "fp-worker")

    async with db_module.async_session_factory() as session:
        channel = Channel(
            team_id=team_id,
            name="c1",
            type="email",
            config_encrypted=encrypt_str('{"recipients": ["a@example.org"]}'),
        )
        session.add(channel)
        await session.flush()
        row = NotificationOutbox(
            alert_event_id=event_id,
            channel_id=channel.id,
            team_id=team_id,
            trigger="firing",
            payload={"alertname": "HighCpu"},
            status="pending",
        )
        session.add(row)
        await session.commit()

        # Soft-delete the channel after the row was already staged --
        # exactly the race the worker has to handle.
        channel.deleted_at = datetime.now(UTC)
        await session.commit()

        registry = ChannelRegistry()
        registry.discover()
        await deliver(row, registry, session)

        assert row.status == "dead"
        assert "deleted" in row.last_error
