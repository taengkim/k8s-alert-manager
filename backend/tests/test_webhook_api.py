"""API-level tests for POST /api/v1/webhook/alertmanager: bearer-token
cluster auth (isolated from cookie/JWT auth entirely) and payload handling.
"""

from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.config import get_settings
from app.models.alert import AlertEvent
from app.models.cluster import Cluster

AM_ALERT = {
    "status": "firing",
    "labels": {
        "alertname": "KamAlwaysFiring",
        "kam_team": "platform",
        "severity": "info",
        "namespace": "kam-demo",
    },
    "annotations": {"summary": "always firing"},
    "startsAt": "2026-09-22T00:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z",
    "fingerprint": "am-fp-1",
    "generatorURL": "http://prom/graph",
}


def _webhook_payload(*alerts: dict) -> dict:
    return {
        "version": "4",
        "groupKey": "{}:{}",
        "status": "firing",
        "alerts": list(alerts) or [AM_ALERT],
    }


async def _default_cluster() -> Cluster:
    async with db_module.async_session_factory() as session:
        result = await session.execute(
            select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
        )
        return result.scalar_one()


async def test_valid_token_returns_200_with_counts(client: AsyncClient, app) -> None:
    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_webhook_payload(),
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "received": 1,
        "created": 1,
        "created_resolved": 0,
        "resolved": 0,
        "repeats": 0,
        "heartbeats_seen": 0,
    }

    async with db_module.async_session_factory() as session:
        row = (await session.execute(select(AlertEvent))).scalar_one()
        assert row.alertname == "KamAlwaysFiring"
        assert row.ends_at is None  # AM zero-value endsAt -> None


async def test_missing_authorization_header_is_401(client: AsyncClient, app) -> None:
    response = await client.post("/api/v1/webhook/alertmanager", json=_webhook_payload())
    assert response.status_code == 401


async def test_invalid_token_is_401(client: AsyncClient, app) -> None:
    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_webhook_payload(),
        headers={"Authorization": "Bearer not-the-right-token"},
    )
    assert response.status_code == 401


async def test_disabled_cluster_is_401(client: AsyncClient, app) -> None:
    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == get_settings().default_cluster_name)
            )
        ).scalar_one()
        cluster.enabled = False
        await session.commit()

    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_webhook_payload(),
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert response.status_code == 401


async def test_malformed_payload_is_400(client: AsyncClient, app) -> None:
    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json={"alerts": [{"status": "firing"}]},  # missing required fields
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert response.status_code == 400


async def test_non_json_body_is_400(client: AsyncClient, app) -> None:
    response = await client.post(
        "/api/v1/webhook/alertmanager",
        content=b"not json",
        headers={
            "Authorization": f"Bearer {get_settings().webhook_token}",
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 400


async def test_unknown_fields_are_ignored(client: AsyncClient, app) -> None:
    alert = dict(AM_ALERT, unknownField="whatever")
    payload = dict(_webhook_payload(alert), somethingElse="ignored")
    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json=payload,
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert response.status_code == 200


async def test_webhook_does_not_require_cookie_auth(client: AsyncClient, app) -> None:
    """The webhook router must have no get_current_user dependency -- a
    request with a valid bearer token but no session cookie must succeed.
    """
    assert "kam_token" not in client.cookies
    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_webhook_payload(),
        headers={"Authorization": f"Bearer {get_settings().webhook_token}"},
    )
    assert response.status_code == 200
