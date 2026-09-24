"""Phase 18 publish-after-commit points: webhook ingest -> alert_created,
ack -> alert_acked, comment -> comment_added -- and the negative case, that
a request whose transaction never commits publishes nothing at all.

Each positive test subscribes directly to the real `app.state.events_hub`
(admin scope, so team filtering can't hide anything) before triggering the
action, then asserts on what actually landed in its queue -- exercising the
real call sites in app/api/webhook.py and app/api/alerts.py rather than
mocking `Hub.publish`.
"""

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.config import get_settings
from app.models.alert import AlertEvent
from app.models.cluster import Cluster
from app.models.team import Team, TeamMembership
from app.services.events_hub import Hub
from tests.conftest import login_as

AM_ALERT = {
    "status": "firing",
    "labels": {"alertname": "KamAlwaysFiring", "severity": "critical", "namespace": "kam-demo"},
    "annotations": {},
    "startsAt": "2026-09-24T00:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "fingerprint": "publish-fp-1",
}


async def _default_cluster_id() -> int:
    async with db_module.async_session_factory() as session:
        result = await session.execute(select(Cluster))
        return result.scalars().first().id


async def _create_team(slug: str) -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(team_id: int, user_id: int, role: str = "member") -> None:
    async with db_module.async_session_factory() as session:
        session.add(TeamMembership(team_id=team_id, user_id=user_id, role=role, origin="manual"))
        await session.commit()


async def _create_event(*, cluster_id: int, fingerprint: str, team_id: int | None) -> int:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        event = AlertEvent(
            cluster_id=cluster_id,
            cluster_name=cluster.name,
            fingerprint=fingerprint,
            status="firing",
            alertname="TestAlert",
            severity="critical",
            namespace="kam-demo",
            labels={"alertname": "TestAlert"},
            annotations={},
            team_id=team_id,
            starts_at=datetime.now(UTC),
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event.id


async def _user_id(client: AsyncClient) -> int:
    return (await client.get("/api/v1/auth/me")).json()["id"]


# -- webhook ingest -> alert_created -----------------------------------------


async def test_webhook_ingest_publishes_alert_created(app: FastAPI, client: AsyncClient) -> None:
    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json={"alerts": [AM_ALERT]},
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert response.status_code == 200

    assert queue.qsize() == 1
    _seq, event = queue.get_nowait()
    assert event["type"] == "alert_created"
    assert event["alertname"] == "KamAlwaysFiring"
    assert event["severity"] == "critical"
    assert event["is_test"] is False


async def test_webhook_repeat_delivery_publishes_nothing_new(
    app: FastAPI, client: AsyncClient
) -> None:
    """A repeat delivery (same firing alert, no status change) is not a
    transition -- app.services.ingest never appends it to
    IngestResult.transitions, so nothing should publish for it."""
    hub: Hub = app.state.events_hub

    first = await client.post(
        "/api/v1/webhook/alertmanager",
        json={"alerts": [AM_ALERT]},
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert first.status_code == 200

    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    repeat = await client.post(
        "/api/v1/webhook/alertmanager",
        json={"alerts": [AM_ALERT]},
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert repeat.status_code == 200
    assert repeat.json()["repeats"] == 1
    assert queue.empty()


# -- ack / unack --------------------------------------------------------


async def test_ack_publishes_alert_acked(app: FastAPI, client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f-ack", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids={team_id}, is_admin=False)

    response = await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    assert response.status_code == 200

    assert queue.qsize() == 1
    _seq, event = queue.get_nowait()
    assert event["type"] == "alert_acked"
    assert event["event_id"] == event_id
    assert event["team_id"] == team_id


async def test_ack_idempotent_second_call_publishes_nothing(
    app: FastAPI, client: AsyncClient
) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f-ack-2", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    await client.post(f"/api/v1/alerts/history/{event_id}/ack")

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids={team_id}, is_admin=False)

    second = await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    assert second.status_code == 200
    assert queue.empty()


async def test_unack_publishes_alert_unacked(app: FastAPI, client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f-unack", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))
    await client.post(f"/api/v1/alerts/history/{event_id}/ack")

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids={team_id}, is_admin=False)

    response = await client.delete(f"/api/v1/alerts/history/{event_id}/ack")
    assert response.status_code == 200

    assert queue.qsize() == 1
    _seq, event = queue.get_nowait()
    assert event["type"] == "alert_unacked"
    assert event["event_id"] == event_id


# -- comments -----------------------------------------------------------


async def test_comment_create_publishes_comment_added(app: FastAPI, client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f-comment", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids={team_id}, is_admin=False)

    response = await client.post(
        f"/api/v1/alerts/history/{event_id}/comments", json={"body": "투입 완료"}
    )
    assert response.status_code == 201

    assert queue.qsize() == 1
    _seq, event = queue.get_nowait()
    assert event["type"] == "comment_added"
    assert event["event_id"] == event_id


# -- never publish before a successful commit --------------------------------


async def test_failed_commit_publishes_nothing(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the transaction never commits (a failure staged before it, here
    simulated by making the audit log write raise), the endpoint's own
    `publish_after_commit` call is never reached -- nothing must land in
    any subscriber's queue, and the DB write itself must have rolled back.
    """
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f-fail", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    async def _raising_log(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr("app.api.alerts.audit.log", _raising_log)

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids={team_id}, is_admin=False)

    with pytest.raises(RuntimeError):
        await client.post(f"/api/v1/alerts/history/{event_id}/ack")

    assert queue.empty()

    async with db_module.async_session_factory() as session:
        event = await session.get(AlertEvent, event_id)
        assert event.acknowledged_at is None
