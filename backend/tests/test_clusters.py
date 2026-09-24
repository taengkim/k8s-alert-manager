"""API-level tests for cluster CRUD (Phase 11): admin RBAC, one-time webhook
token issuance + rotation, credential encryption at rest, slug immutability,
delete-with-history guard, and admin vs non-admin field visibility.
"""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.config import get_settings
from app.models.alert import AlertEvent
from app.models.audit import AuditLog
from app.models.cluster import Cluster
from app.security import decrypt_str, hash_token
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


def _empty_webhook_payload() -> dict:
    return {"version": "4", "groupKey": "{}:{}", "status": "firing", "alerts": []}


async def _create_cluster(client: AsyncClient, name: str = "staging", **overrides) -> dict:
    body = {
        "name": name,
        "display_name": name.title(),
        "prometheus_url": "http://prom",
        "alertmanager_url": "http://am",
        **overrides,
    }
    response = await client.post("/api/v1/clusters", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def test_default_cluster_is_seeded_by_bootstrap(client: AsyncClient) -> None:
    settings = get_settings()
    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(
                select(Cluster).where(Cluster.name == settings.default_cluster_name)
            )
        ).scalar_one()
        assert cluster.prometheus_url == settings.prometheus_url
        assert cluster.alertmanager_url == settings.alertmanager_url
        assert cluster.webhook_token_hash == hash_token(settings.webhook_token)


# -- GET /clusters ---------------------------------------------------------


async def test_get_clusters_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/clusters")
    assert response.status_code == 401


async def test_non_admin_sees_only_public_fields(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    response = await client.get("/api/v1/clusters")
    assert response.status_code == 200
    entry = response.json()[0]
    assert set(entry.keys()) == {
        "id",
        "name",
        "display_name",
        "enabled",
        "health",
        "heartbeat_state",
        "last_heartbeat_at",
    }
    assert entry["name"] == get_settings().default_cluster_name


async def test_admin_sees_connection_fields_but_never_credentials(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.get("/api/v1/clusters")
    entry = response.json()[0]
    for field in (
        "prometheus_url",
        "alertmanager_url",
        "grafana_url",
        "rules_namespace",
        "k8s_auth_kind",
        "heartbeat_enabled",
        "heartbeat_alertname",
        "heartbeat_timeout_seconds",
        "heartbeat_team_id",
    ):
        assert field in entry
    assert "credentials_encrypted" not in entry
    assert "webhook_token_hash" not in entry
    assert "webhook_token" not in entry


# -- POST /clusters ----------------------------------------------------------


async def test_create_requires_admin(client: AsyncClient) -> None:
    await login_as(client, username="bob")
    response = await client.post(
        "/api/v1/clusters",
        json={
            "name": "staging",
            "display_name": "Staging",
            "prometheus_url": "http://prom",
            "alertmanager_url": "http://am",
        },
    )
    assert response.status_code == 403


async def test_create_returns_webhook_token_once_and_stores_only_hash(
    client: AsyncClient,
) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    body = await _create_cluster(client)

    assert body["webhook_token"]
    assert "am_config_snippet" in body
    assert body["webhook_token"] in body["am_config_snippet"]
    assert "receivers:" in body["am_config_snippet"]

    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(select(Cluster).where(Cluster.name == "staging"))
        ).scalar_one()
        assert cluster.webhook_token_hash == hash_token(body["webhook_token"])

    # A subsequent GET never re-exposes the token or its hash.
    get_response = await client.get("/api/v1/clusters")
    staging = next(c for c in get_response.json() if c["name"] == "staging")
    assert "webhook_token" not in staging
    assert "webhook_token_hash" not in staging


async def test_create_writes_audit_row(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    await _create_cluster(client, name="staging-audit")

    async with db_module.async_session_factory() as session:
        row = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "cluster.create", AuditLog.object_ref == "staging-audit"
                )
            )
        ).scalar_one()
        assert row.object_type == "cluster"


async def test_create_encrypts_kubeconfig_credentials_at_rest(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    kubeconfig_yaml = "apiVersion: v1\nkind: Config\nclusters: []\n"
    await _create_cluster(
        client,
        name="staging-kc",
        k8s_auth_kind="kubeconfig",
        credentials=kubeconfig_yaml,
    )

    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(select(Cluster).where(Cluster.name == "staging-kc"))
        ).scalar_one()
        assert cluster.credentials_encrypted is not None
        assert kubeconfig_yaml not in cluster.credentials_encrypted
        assert decrypt_str(cluster.credentials_encrypted) == kubeconfig_yaml


async def test_create_token_auth_encrypts_token_and_ca(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    await _create_cluster(
        client,
        name="staging-tok",
        k8s_auth_kind="token",
        k8s_api_url="https://cluster.invalid:6443",
        credentials={"token": "super-secret-token", "ca_cert": "-----BEGIN CERTIFICATE-----"},
    )

    async with db_module.async_session_factory() as session:
        cluster = (
            await session.execute(select(Cluster).where(Cluster.name == "staging-tok"))
        ).scalar_one()
        assert "super-secret-token" not in cluster.credentials_encrypted
        assert decrypt_str(cluster.credentials_encrypted) == (
            '{"token": "super-secret-token", "ca_cert": "-----BEGIN CERTIFICATE-----"}'
        )


async def test_create_token_auth_requires_token_field(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.post(
        "/api/v1/clusters",
        json={
            "name": "staging-badtok",
            "display_name": "Bad Tok",
            "k8s_auth_kind": "token",
            "credentials": {"not_token": "x"},
            "prometheus_url": "http://prom",
            "alertmanager_url": "http://am",
        },
    )
    assert response.status_code == 422


async def test_create_incluster_auth_rejects_credentials(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.post(
        "/api/v1/clusters",
        json={
            "name": "staging-incluster",
            "display_name": "Incluster",
            "k8s_auth_kind": "incluster",
            "credentials": "should not be here",
            "prometheus_url": "http://prom",
            "alertmanager_url": "http://am",
        },
    )
    assert response.status_code == 422


async def test_create_duplicate_name_is_409(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.post(
        "/api/v1/clusters",
        json={
            "name": get_settings().default_cluster_name,
            "display_name": "dup",
            "prometheus_url": "http://prom",
            "alertmanager_url": "http://am",
        },
    )
    assert response.status_code == 409


# -- PATCH /clusters/{id} ----------------------------------------------------


async def test_update_requires_admin(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client)

    await login_as(client, username="bob")
    response = await client.patch(f"/api/v1/clusters/{created['id']}", json={"display_name": "x"})
    assert response.status_code == 403


async def test_update_rejects_name_change(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client)

    response = await client.patch(f"/api/v1/clusters/{created['id']}", json={"name": "renamed"})
    assert response.status_code == 422


async def test_update_display_name_and_grafana_url(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client)

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}",
        json={"display_name": "Staging 2", "grafana_url": "https://grafana.example.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Staging 2"
    assert body["grafana_url"] == "https://grafana.example.com"
    assert "webhook_token" not in body


async def test_update_writes_cluster_update_audit_row(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client, name="staging-upd-audit")

    await client.patch(f"/api/v1/clusters/{created['id']}", json={"display_name": "renamed"})

    async with db_module.async_session_factory() as session:
        row = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "cluster.update",
                    AuditLog.object_ref == "staging-upd-audit",
                )
            )
        ).scalar_one_or_none()
        assert row is not None


async def test_update_omitting_credentials_preserves_stored_secret(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    kubeconfig_yaml = "apiVersion: v1\nkind: Config\nclusters: []\n"
    created = await _create_cluster(
        client,
        name="staging-preserve-creds",
        k8s_auth_kind="kubeconfig",
        credentials=kubeconfig_yaml,
    )

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"display_name": "renamed only"}
    )
    assert response.status_code == 200

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        assert cluster.credentials_encrypted is not None
        assert decrypt_str(cluster.credentials_encrypted) == kubeconfig_yaml


async def test_update_credentials_null_clears_stored_secret(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(
        client,
        name="staging-clear-creds",
        k8s_auth_kind="kubeconfig",
        credentials="apiVersion: v1\nkind: Config\nclusters: []\n",
    )

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"credentials": None}
    )
    assert response.status_code == 200

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        assert cluster.credentials_encrypted is None


async def test_update_kind_and_credentials_together_validated_against_new_kind(
    client: AsyncClient,
) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(
        client,
        name="staging-switch-kind",
        k8s_auth_kind="kubeconfig",
        credentials="apiVersion: v1\nkind: Config\nclusters: []\n",
    )

    # Wrong shape for the NEW kind (a kubeconfig string handed to 'token')
    # must 422 -- proves validation runs against the kind this PATCH is
    # switching *to*, not the cluster's current one.
    bad_response = await client.patch(
        f"/api/v1/clusters/{created['id']}",
        json={
            "k8s_auth_kind": "token",
            "credentials": "apiVersion: v1\nkind: Config\n",
            "k8s_api_url": "https://cluster.invalid:6443",
        },
    )
    assert bad_response.status_code == 422

    good_response = await client.patch(
        f"/api/v1/clusters/{created['id']}",
        json={
            "k8s_auth_kind": "token",
            "credentials": {"token": "new-token-value"},
            "k8s_api_url": "https://cluster.invalid:6443",
        },
    )
    assert good_response.status_code == 200
    assert good_response.json()["k8s_auth_kind"] == "token"

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        assert cluster.k8s_auth_kind == "token"
        assert decrypt_str(cluster.credentials_encrypted) == (
            '{"token": "new-token-value", "ca_cert": null}'
        )


async def test_update_kind_change_to_token_without_credentials_is_422(
    client: AsyncClient,
) -> None:
    # 'token' is the one kind with no valid "no credentials" state -- unlike
    # 'incluster'/'kubeconfig' below, there's nothing sensible to fall back
    # to, so this must still be refused outright.
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(
        client,
        name="staging-kind-switch-no-creds",
        k8s_auth_kind="kubeconfig",
        credentials="apiVersion: v1\nkind: Config\nclusters: []\n",
    )

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"k8s_auth_kind": "token"}
    )
    assert response.status_code == 422
    assert "자격증명" in response.json()["detail"]

    # Refused before any mutation -- the cluster's kind is untouched.
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        assert cluster.k8s_auth_kind == "kubeconfig"


async def test_update_kind_change_with_no_stored_creds_at_all_is_allowed(
    client: AsyncClient,
) -> None:
    # No credentials ever stored (kubeconfig's dev-default: use the host's
    # own kubeconfig) -- switching to 'incluster', which also needs none,
    # must not be blocked.
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client, name="staging-kind-switch-empty")

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"k8s_auth_kind": "incluster"}
    )
    assert response.status_code == 200
    assert response.json()["k8s_auth_kind"] == "incluster"


async def test_update_kubeconfig_to_incluster_without_credentials_clears_stored_secret(
    client: AsyncClient,
) -> None:
    # 'incluster' never reads stored credentials, so this switch has a
    # perfectly valid "no credentials supplied" state (unlike 'token') --
    # but the stale kubeconfig ciphertext must not silently survive the
    # switch as if it still meant something.
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(
        client,
        name="staging-kc-to-incluster",
        k8s_auth_kind="kubeconfig",
        credentials="apiVersion: v1\nkind: Config\nclusters: []\n",
    )

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"k8s_auth_kind": "incluster"}
    )
    assert response.status_code == 200
    assert response.json()["k8s_auth_kind"] == "incluster"

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        assert cluster.k8s_auth_kind == "incluster"
        assert cluster.credentials_encrypted is None


async def test_update_token_to_incluster_without_credentials_clears_stored_secret(
    client: AsyncClient,
) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(
        client,
        name="staging-tok-to-incluster",
        k8s_auth_kind="token",
        k8s_api_url="https://cluster.invalid:6443",
        credentials={"token": "some-token"},
    )

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"k8s_auth_kind": "incluster"}
    )
    assert response.status_code == 200
    assert response.json()["k8s_auth_kind"] == "incluster"

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        assert cluster.k8s_auth_kind == "incluster"
        assert cluster.credentials_encrypted is None


async def test_update_token_to_kubeconfig_without_credentials_uses_host_default(
    client: AsyncClient,
) -> None:
    # 'kubeconfig' with no credentials supplied is its own valid state (the
    # dev-default: use the host's own kubeconfig) -- the stale token+ca_cert
    # blob must not survive the switch as if it were still a usable
    # kubeconfig.
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(
        client,
        name="staging-tok-to-kubeconfig",
        k8s_auth_kind="token",
        k8s_api_url="https://cluster.invalid:6443",
        credentials={"token": "some-token"},
    )

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"k8s_auth_kind": "kubeconfig"}
    )
    assert response.status_code == 200
    assert response.json()["k8s_auth_kind"] == "kubeconfig"

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        assert cluster.k8s_auth_kind == "kubeconfig"
        assert cluster.credentials_encrypted is None


async def test_rotate_webhook_token_invalidates_old_token(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client, name="staging-rotate")
    old_token = created["webhook_token"]

    ok = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_empty_webhook_payload(),
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert ok.status_code == 200

    response = await client.patch(
        f"/api/v1/clusters/{created['id']}", json={"rotate_webhook_token": True}
    )
    assert response.status_code == 200
    body = response.json()
    new_token = body["webhook_token"]
    assert new_token and new_token != old_token
    assert new_token in body["am_config_snippet"]

    old_after = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_empty_webhook_payload(),
        headers={"Authorization": f"Bearer {old_token}"},
    )
    assert old_after.status_code == 401

    new_after = await client.post(
        "/api/v1/webhook/alertmanager",
        json=_empty_webhook_payload(),
        headers={"Authorization": f"Bearer {new_token}"},
    )
    assert new_after.status_code == 200

    async with db_module.async_session_factory() as session:
        row = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "cluster.rotate_token",
                    AuditLog.object_ref == "staging-rotate",
                )
            )
        ).scalar_one_or_none()
        assert row is not None


# -- Phase 17: heartbeat-missing reset on disable ----------------------------


async def _setup_missing_cluster_with_notify_rule(
    client: AsyncClient, name: str
) -> tuple[int, int, int]:
    """Admin-creates `name`, then directly wires up (via ORM) a team with a
    notify_on_resolved rule + channel, a 'missing' heartbeat_state, and an
    open synthetic KamClusterHeartbeatLost event -- the exact state a real
    heartbeat sweep timeout would have left behind. Returns
    (cluster_id, event_id, channel_id).
    """
    from app.channels.email import EmailConfig
    from app.models.channel import Channel
    from app.security import encrypt_str
    from app.services.ingest import HEARTBEAT_LOST_ALERTNAME, heartbeat_lost_fingerprint

    created = await _create_cluster(client, name=name)
    cluster_id = created["id"]

    async with db_module.async_session_factory() as session:
        from app.models.routing import RoutingRule
        from app.models.team import Team

        team = Team(slug=f"{name}-team", name=f"{name} Team")
        session.add(team)
        await session.flush()

        channel = Channel(
            team_id=team.id,
            name=f"{name}-channel",
            type="email",
            config_encrypted=encrypt_str(
                EmailConfig(recipients=["oncall@example.org"]).model_dump_json()
            ),
        )
        session.add(channel)
        await session.flush()

        rule = RoutingRule(
            team_id=team.id,
            name=f"{name}-rule",
            action="notify",
            notify_on_firing=True,
            notify_on_resolved=True,
            channels=[channel],
        )
        session.add(rule)
        await session.flush()

        cluster = await session.get(Cluster, cluster_id)
        cluster.heartbeat_state = "missing"
        cluster.heartbeat_team_id = team.id

        event = AlertEvent(
            cluster_id=cluster.id,
            cluster_name=cluster.name,
            fingerprint=heartbeat_lost_fingerprint(cluster.id),
            status="firing",
            alertname=HEARTBEAT_LOST_ALERTNAME,
            severity="critical",
            namespace=None,
            labels={"alertname": HEARTBEAT_LOST_ALERTNAME, "kam_team": team.slug},
            annotations={"description": "test"},
            team_id=team.id,
            starts_at=datetime(2026, 9, 22, 0, 0, 0, tzinfo=UTC),
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    return cluster_id, event_id, channel.id


async def test_disabling_cluster_while_missing_resets_heartbeat_and_resolves_event(
    client: AsyncClient,
) -> None:
    """Webhook auth (app/api/webhook.py) requires enabled=True, so disabling
    a cluster while its heartbeat is 'missing' would otherwise leave it
    permanently stuck there -- no future heartbeat could ever arrive to
    resolve it. The reset must route the resolution through the normal
    route_event path, so a notify_on_resolved rule still fires a recovery
    notification (an outbox row for the rule's channel).
    """
    from app.models.outbox import NotificationOutbox

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    cluster_id, event_id, channel_id = await _setup_missing_cluster_with_notify_rule(
        client, "disable-missing"
    )

    response = await client.patch(f"/api/v1/clusters/{cluster_id}", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["heartbeat_state"] == "unknown"

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.enabled is False
        assert cluster.heartbeat_state == "unknown"

        event = await session.get(AlertEvent, event_id)
        assert event.status == "resolved"
        assert event.ends_at is not None

        outbox_rows = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event_id)
            )
        ).scalars().all()
        assert len(outbox_rows) == 1
        assert outbox_rows[0].channel_id == channel_id

        audit_row = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "cluster.update", AuditLog.object_ref == "disable-missing"
                )
            )
        ).scalar_one()
        assert audit_row.detail == {"heartbeat_reset_from_missing": True}


async def test_disabling_heartbeat_enabled_while_missing_resets_heartbeat_and_resolves_event(
    client: AsyncClient,
) -> None:
    """Same class of fix as disabling the cluster itself: turning
    heartbeat_enabled off while 'missing' means app.services.ingest's
    heartbeat hook will never run again for this cluster either, so a real
    Watchdog heartbeat could never resolve it -- must reset + resolve just
    the same.
    """
    from app.models.outbox import NotificationOutbox

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    cluster_id, event_id, channel_id = await _setup_missing_cluster_with_notify_rule(
        client, "disable-hb-missing"
    )

    response = await client.patch(
        f"/api/v1/clusters/{cluster_id}", json={"heartbeat_enabled": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["heartbeat_state"] == "unknown"

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        assert cluster.heartbeat_enabled is False
        assert cluster.heartbeat_state == "unknown"

        event = await session.get(AlertEvent, event_id)
        assert event.status == "resolved"
        assert event.ends_at is not None

        outbox_rows = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event_id)
            )
        ).scalars().all()
        assert len(outbox_rows) == 1
        assert outbox_rows[0].channel_id == channel_id


async def test_disabling_cluster_not_missing_has_no_heartbeat_side_effect(
    client: AsyncClient,
) -> None:
    """The reset/resolve behavior is gated on heartbeat_state=='missing' --
    disabling an 'ok' (or 'unknown') cluster is a plain disable, no synthetic
    event to resolve, no state reset.
    """
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client, name="disable-plain")

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, created["id"])
        cluster.heartbeat_state = "ok"
        cluster.last_heartbeat_at = datetime.now(UTC)
        await session.commit()

    response = await client.patch(f"/api/v1/clusters/{created['id']}", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert response.json()["heartbeat_state"] == "ok"

    async with db_module.async_session_factory() as session:
        audit_row = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "cluster.update", AuditLog.object_ref == "disable-plain"
                )
            )
        ).scalar_one()
        assert audit_row.detail is None


# -- DELETE /clusters/{id} ---------------------------------------------------


async def test_delete_requires_admin(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client)

    await login_as(client, username="bob")
    response = await client.delete(f"/api/v1/clusters/{created['id']}")
    assert response.status_code == 403


async def test_delete_without_history_succeeds(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client, name="staging-del")

    response = await client.delete(f"/api/v1/clusters/{created['id']}")
    assert response.status_code == 204

    async with db_module.async_session_factory() as session:
        assert await session.get(Cluster, created["id"]) is None


async def test_delete_with_history_is_409(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    created = await _create_cluster(client, name="staging-hist")

    async with db_module.async_session_factory() as session:
        session.add(
            AlertEvent(
                cluster_id=created["id"],
                cluster_name=created["name"],
                fingerprint="fp1",
                status="firing",
                alertname="X",
                labels={},
                annotations={},
                starts_at=datetime.now(UTC),
            )
        )
        await session.commit()

    response = await client.delete(f"/api/v1/clusters/{created['id']}")
    assert response.status_code == 409
    assert "비활성화" in response.json()["detail"]

    async with db_module.async_session_factory() as session:
        assert await session.get(Cluster, created["id"]) is not None
