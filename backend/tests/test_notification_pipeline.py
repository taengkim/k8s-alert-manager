"""End-to-end test of the full Phase 9 pipeline: an Alertmanager webhook
POST creates an alert event, routing stages an outbox row for it inside
that same request's transaction, and a worker tick delivers it through a
registered (fake, for this test) channel.

No real SMTP/respx mocking needed -- a fake channel type is registered
directly on a `ChannelRegistry`, the same mechanism a third-party plugin
would use.
"""

from typing import ClassVar

from httpx import AsyncClient
from pydantic import BaseModel
from sqlalchemy import select

import app.db as db_module
from app.channels.base import AlertNotification, NotificationChannel, RenderedMessage
from app.channels.registry import ChannelRegistry
from app.config import get_settings
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.team import Team
from app.security import encrypt_str
from app.worker.outbox import run_tick

AM_ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "KamCriticalDemo",
        "kam_team": "platform",
        "severity": "critical",
        "namespace": "kam-demo",
    },
    "annotations": {"summary": "critical demo alert", "runbook_url": "https://runbooks/x"},
    "startsAt": "2026-09-22T00:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "fingerprint": "am-e2e-fp-1",
    "generatorURL": "http://prom/graph",
}


def _webhook_payload(*alerts: dict) -> dict:
    return {"version": "4", "groupKey": "{}:{}", "status": "firing", "alerts": list(alerts) or [AM_ALERT]}


async def _default_cluster() -> Cluster:
    async with db_module.async_session_factory() as session:
        result = await session.execute(
            select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
        )
        return result.scalar_one()


class _FakeConfig(BaseModel):
    marker: str = "ok"


class _FakeChannel(NotificationChannel):
    type_name = "e2e-fake"
    display_name = "E2E Fake"
    config_schema = _FakeConfig

    sent: ClassVar[list[AlertNotification]] = []
    sent_messages: ClassVar[list[RenderedMessage]] = []

    async def send(self, notification: AlertNotification, msg: RenderedMessage) -> None:
        _FakeChannel.sent.append(notification)
        _FakeChannel.sent_messages.append(msg)


async def test_webhook_to_delivery_end_to_end(client: AsyncClient, app) -> None:
    _FakeChannel.sent.clear()
    _FakeChannel.sent_messages.clear()

    await _default_cluster()  # sanity: the seeded default cluster exists
    async with db_module.async_session_factory() as session:
        team = Team(slug="platform", name="Platform")
        session.add(team)
        await session.flush()

        channel = Channel(
            team_id=team.id,
            name="e2e-channel",
            type=_FakeChannel.type_name,
            config_encrypted=encrypt_str('{"marker": "ok"}'),
        )
        session.add(channel)
        await session.flush()

        # Direct insert (bypassing the routes API, which is covered
        # separately in test_routes_api.py) -- a plain notify-all rule for
        # this team, routed to the fake channel.
        rule = RoutingRule(team_id=team.id, name="notify-all", action="notify", channels=[channel])
        session.add(rule)
        await session.commit()

    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_webhook_payload(),
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert response.status_code == 200
    assert response.json()["created"] == 1

    async with db_module.async_session_factory() as session:
        event = (
            await session.execute(
                select(AlertEvent).where(AlertEvent.fingerprint == AM_ALERT["fingerprint"])
            )
        ).scalar_one()
        assert event.team_id is not None

        outbox_row = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event.id)
            )
        ).scalar_one()
        assert outbox_row.status == "pending"
        assert outbox_row.trigger == "firing"
        assert outbox_row.payload["alertname"] == "KamCriticalDemo"
        assert outbox_row.payload["runbook_url"] == "https://runbooks/x"

    registry = ChannelRegistry()
    registry.discover()
    registry._register(_FakeChannel, source="test")

    claimed = await run_tick(db_module.async_session_factory, registry, "e2e-worker")
    assert claimed == 1

    async with db_module.async_session_factory() as session:
        outbox_row = await session.get(NotificationOutbox, outbox_row.id)
        assert outbox_row.status == "delivered"
        assert outbox_row.delivered_at is not None

    assert len(_FakeChannel.sent) == 1
    assert _FakeChannel.sent[0].alertname == "KamCriticalDemo"
    assert _FakeChannel.sent[0].team_slug == "platform"

    # No routing rule/channel template_id was set, and _FakeChannel declares
    # no default_templates -- the app-wide default template renders here.
    assert len(_FakeChannel.sent_messages) == 1
    assert "KamCriticalDemo" in _FakeChannel.sent_messages[0].title
    assert "critical" in _FakeChannel.sent_messages[0].title
