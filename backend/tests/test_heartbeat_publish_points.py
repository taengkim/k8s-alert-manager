"""Phase 18 publish-after-commit points for the heartbeat-lost synthetic
alert's three paths (raised in code review -- the highest-severity
synthetic this live feed exists for was reaching the hub via NONE of
them before this fix):

1. Sweep CREATE (ok->missing edge, app.worker.heartbeat.sweep) -- publishes
   alert_created when given a hub, and must not crash when it isn't.
2. Recovery via a real webhook heartbeat delivery (missing->ok, through
   app.services.ingest._ingest_one) -- publishes alert_resolved.
3. Recovery via an admin PATCH disabling a 'missing' cluster
   (app/api/clusters.py's update_cluster) -- publishes alert_resolved.

Each positive test subscribes directly to the real app.state.events_hub
(admin scope) before triggering the action, mirroring
tests/test_events_publish_points.py. The negative test confirms the same
never-publish-before-commit discipline applies to the admin-PATCH path too.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

import app.db as db_module
from app.models.cluster import Cluster
from app.security import hash_token
from app.services.events_hub import Hub
from app.services.ingest import HEARTBEAT_LOST_ALERTNAME
from app.worker.heartbeat import sweep
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


def _stale(seconds: int) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


async def _create_cluster(
    session,
    name: str = "hb-cluster",
    *,
    webhook_token_hash: str | None = None,
    heartbeat_timeout_seconds: int = 60,
    last_heartbeat_at: datetime | None = None,
) -> Cluster:
    cluster = Cluster(
        name=name,
        display_name=name.title(),
        prometheus_url="http://prom",
        alertmanager_url="http://am",
        webhook_token_hash=webhook_token_hash or f"hash-{name}",
        # The model default is 'unknown' (never alarms) -- these tests all
        # want a cluster the sweep actually considers a candidate, i.e.
        # already past its first heartbeat.
        heartbeat_state="ok",
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
        last_heartbeat_at=last_heartbeat_at,
    )
    session.add(cluster)
    await session.flush()
    return cluster


# -- 1. sweep CREATE path -----------------------------------------------


async def test_sweep_with_hub_publishes_alert_created(app: FastAPI) -> None:
    async with db_module.async_session_factory() as session:
        await _create_cluster(
            session, name="hb-sweep-create", heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        await session.commit()

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    summary = await sweep(db_module.async_session_factory, hub=hub)
    assert summary["went_missing"] == ["hb-sweep-create"]

    assert queue.qsize() == 1
    _seq, event = queue.get_nowait()
    assert event["type"] == "alert_created"
    assert event["alertname"] == HEARTBEAT_LOST_ALERTNAME
    assert event["severity"] == "critical"


async def test_sweep_without_hub_does_not_crash(app: FastAPI) -> None:
    """`hub` defaults to None -- the standalone worker process (and any
    caller that just doesn't have one) must work exactly as before this
    phase, with no publish attempted at all.
    """
    async with db_module.async_session_factory() as session:
        await _create_cluster(
            session, name="hb-sweep-no-hub", heartbeat_timeout_seconds=60, last_heartbeat_at=_stale(120)
        )
        await session.commit()

    summary = await sweep(db_module.async_session_factory)
    assert summary["went_missing"] == ["hb-sweep-no-hub"]


# -- 2. recovery via a real webhook heartbeat delivery -----------------------


async def test_webhook_recovery_publishes_alert_resolved(app: FastAPI, client: AsyncClient) -> None:
    token = "hb-recovery-token"
    async with db_module.async_session_factory() as session:
        await _create_cluster(
            session,
            name="hb-webhook-recovery",
            webhook_token_hash=hash_token(token),
            heartbeat_timeout_seconds=60,
            last_heartbeat_at=_stale(120),
        )
        await session.commit()

    # Drive it missing first (creates the synthetic firing event) -- no hub,
    # since only the recovery half is under test here.
    await sweep(db_module.async_session_factory)

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    response = await client.post(
        "/api/v1/webhook/alertmanager",
        json={
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "Watchdog"},
                    "startsAt": datetime.now(UTC).isoformat(),
                    "fingerprint": "wd-recovery-1",
                }
            ]
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["heartbeats_seen"] == 1

    assert queue.qsize() == 1
    _seq, event = queue.get_nowait()
    assert event["type"] == "alert_resolved"
    assert event["alertname"] == HEARTBEAT_LOST_ALERTNAME


# -- 3. recovery via admin PATCH disabling a 'missing' cluster ---------------


async def test_admin_disable_publishes_alert_resolved(app: FastAPI, client: AsyncClient) -> None:
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session,
            name="hb-admin-disable",
            heartbeat_timeout_seconds=60,
            last_heartbeat_at=_stale(120),
        )
        await session.commit()
        cluster_id = cluster.id

    await sweep(db_module.async_session_factory)  # drives it missing, no hub

    await login_as(client, username="admin-user", group_dns=[ADMIN_DN])

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    response = await client.patch(f"/api/v1/clusters/{cluster_id}", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["heartbeat_state"] == "unknown"

    assert queue.qsize() == 1
    _seq, event = queue.get_nowait()
    assert event["type"] == "alert_resolved"
    assert event["alertname"] == HEARTBEAT_LOST_ALERTNAME


async def test_admin_disable_failed_commit_publishes_nothing(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same never-publish-before-commit discipline as every other publish
    site: if the transaction fails before its commit (here, the audit log
    write is made to raise), the resolved-event mutation staged by
    resolve_heartbeat_lost_event rolls back along with everything else, and
    nothing reaches any subscriber's queue.
    """
    async with db_module.async_session_factory() as session:
        cluster = await _create_cluster(
            session,
            name="hb-admin-fail",
            heartbeat_timeout_seconds=60,
            last_heartbeat_at=_stale(120),
        )
        await session.commit()
        cluster_id = cluster.id

    await sweep(db_module.async_session_factory)

    await login_as(client, username="admin-user", group_dns=[ADMIN_DN])

    async def _raising_log(*args, **kwargs):
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr("app.api.clusters.audit.log", _raising_log)

    hub: Hub = app.state.events_hub
    _sub_id, queue = hub.subscribe(team_ids=set(), is_admin=True)

    with pytest.raises(RuntimeError):
        await client.patch(f"/api/v1/clusters/{cluster_id}", json={"enabled": False})

    assert queue.empty()

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_state == "missing"  # never flipped -- rolled back
