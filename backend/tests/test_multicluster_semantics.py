"""API-level multi-cluster semantics (Phase 11 brief §7): the same alert
(identical fingerprint + startsAt) delivered through two different
clusters' webhook tokens must land as two separate alert_events rows --
re-confirming the composite (cluster_id, fingerprint, starts_at) identity
end-to-end through the real webhook + auth path, not just at the ingest
service layer (see test_ingest.py for that). Also covers the ClusterFilter
read path (cluster_id[] on history) and a routing rule scoped to a single
cluster only routing that cluster's events.
"""

from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.channels.email import EmailConfig
from app.config import get_settings
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.team import Team
from app.security import encrypt_str
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"

SHARED_FINGERPRINT = "shared-fp-across-clusters"
SHARED_STARTS_AT = "2026-09-23T00:00:00Z"


def _alert(team_slug: str) -> dict:
    return {
        "status": "firing",
        "labels": {
            "alertname": "SharedAlert",
            "kam_team": team_slug,
            "severity": "critical",
            "namespace": "kam-demo",
        },
        "annotations": {"summary": "same alert, two clusters"},
        "startsAt": SHARED_STARTS_AT,
        "fingerprint": SHARED_FINGERPRINT,
        "generatorURL": "http://prom/graph",
    }


def _webhook_payload(team_slug: str) -> dict:
    return {
        "version": "4",
        "groupKey": "{}:{}",
        "status": "firing",
        "alerts": [_alert(team_slug)],
    }


async def _create_second_cluster(client: AsyncClient, name: str = "second-cluster") -> dict:
    response = await client.post(
        "/api/v1/clusters",
        json={
            "name": name,
            "display_name": name,
            "prometheus_url": "http://prom-2",
            "alertmanager_url": "http://am-2",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_same_alert_via_two_cluster_tokens_creates_two_events(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    second = await _create_second_cluster(client)

    default_token = get_settings().webhook_token
    second_token = second["webhook_token"]

    response_a = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_webhook_payload("platform"),
        headers={"Authorization": f"Bearer {default_token}"},
    )
    assert response_a.status_code == 200
    assert response_a.json()["created"] == 1

    response_b = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_webhook_payload("platform"),
        headers={"Authorization": f"Bearer {second_token}"},
    )
    assert response_b.status_code == 200
    assert response_b.json()["created"] == 1

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(
                select(AlertEvent).where(AlertEvent.fingerprint == SHARED_FINGERPRINT)
            )
        ).scalars().all()
        assert len(rows) == 2
        assert {r.cluster_id for r in rows} == {
            (
                await session.execute(
                    select(AlertEvent.cluster_id).where(AlertEvent.cluster_name == "local")
                )
            ).scalar_one(),
            second["id"],
        }

    # ClusterFilter read path: cluster_id[] on history scopes each event to
    # its own cluster.
    history_b = await client.get(f"/api/v1/alerts/history?cluster_id={second['id']}")
    assert history_b.status_code == 200
    assert len(history_b.json()["items"]) == 1
    assert history_b.json()["items"][0]["cluster_id"] == second["id"]

    async with db_module.async_session_factory() as session:
        default_cluster_id = (
            await session.execute(
                select(AlertEvent.cluster_id).where(AlertEvent.cluster_name == "local")
            )
        ).scalar_one()
    history_a = await client.get(f"/api/v1/alerts/history?cluster_id={default_cluster_id}")
    assert len(history_a.json()["items"]) == 1
    assert history_a.json()["items"][0]["cluster_id"] == default_cluster_id


async def test_routing_rule_scoped_to_one_cluster_only_routes_that_clusters_events(
    client: AsyncClient,
) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    second = await _create_second_cluster(client, name="second-cluster-routing")

    async with db_module.async_session_factory() as session:
        default_cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
            )
        ).scalar_one()
        default_cluster_id = default_cluster.id

        team = Team(slug="routed-team", name="Routed Team")
        session.add(team)
        await session.flush()

        channel = Channel(
            team_id=team.id,
            name="email-1",
            type="email",
            config_encrypted=encrypt_str(EmailConfig(recipients=["ops@example.org"]).model_dump_json()),
        )
        session.add(channel)
        await session.flush()

        rule = RoutingRule(
            team_id=team.id,
            name="cluster-a-only",
            action="notify",
            clusters=[default_cluster_id],
            channels=[channel],
        )
        session.add(rule)
        await session.commit()

    fp = "routing-scope-fp"
    payload_a = {
        "version": "4",
        "groupKey": "{}:{}",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "ScopedAlert",
                    "kam_team": "routed-team",
                    "severity": "critical",
                },
                "annotations": {},
                "startsAt": "2026-09-23T01:00:00Z",
                "fingerprint": fp,
            }
        ],
    }
    payload_b = {**payload_a, "alerts": [{**payload_a["alerts"][0], "fingerprint": fp + "-b"}]}

    resp_a = await client.post(
        "/api/v1/webhook/alertmanager",
        json=payload_a,
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert resp_a.status_code == 200

    resp_b = await client.post(
        "/api/v1/webhook/alertmanager",
        json=payload_b,
        headers={"Authorization": f"Bearer {second['webhook_token']}"},
    )
    assert resp_b.status_code == 200

    async with db_module.async_session_factory() as session:
        event_a = (
            await session.execute(select(AlertEvent).where(AlertEvent.fingerprint == fp))
        ).scalar_one()
        event_b = (
            await session.execute(
                select(AlertEvent).where(AlertEvent.fingerprint == fp + "-b")
            )
        ).scalar_one()

        outbox_a = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event_a.id)
            )
        ).scalars().all()
        outbox_b = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event_b.id)
            )
        ).scalars().all()

        assert len(outbox_a) == 1, "cluster A's event must be routed (rule.clusters includes it)"
        assert len(outbox_b) == 0, "cluster B's event must NOT be routed (rule.clusters excludes it)"
