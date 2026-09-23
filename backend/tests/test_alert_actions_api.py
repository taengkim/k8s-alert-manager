"""API tests for Phase 10's ack/unack, assignee, comment thread, and the
live-alerts ack-status batch endpoint (app/api/alerts.py additions).

Test-alert firing/resolve-test live in tests/test_test_alert_api.py.
"""

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.audit import AuditLog
from app.models.cluster import Cluster
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _fresh_client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


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


async def _default_cluster_id() -> int:
    async with db_module.async_session_factory() as session:
        result = await session.execute(select(Cluster))
        return result.scalars().first().id


async def _create_event(
    *,
    cluster_id: int,
    fingerprint: str,
    alertname: str = "TestAlert",
    team_id: int | None,
    status: str = "firing",
) -> int:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        event = AlertEvent(
            cluster_id=cluster_id,
            cluster_name=cluster.name,
            fingerprint=fingerprint,
            status=status,
            alertname=alertname,
            severity="critical",
            namespace="kam-demo",
            labels={"alertname": alertname},
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


# -- ack / unack ------------------------------------------------------------


async def test_ack_requires_auth(client: AsyncClient) -> None:
    response = await client.post("/api/v1/alerts/history/1/ack")
    assert response.status_code == 401


async def test_ack_forbidden_for_non_member(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="carol")
    response = await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    assert response.status_code == 403


async def test_ack_unassigned_event_requires_admin(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=None)

    await login_as(client, username="bob")
    response = await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    assert response.status_code == 403


async def test_ack_unassigned_event_allowed_for_admin(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=None)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    response = await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    assert response.status_code == 200
    assert response.json()["acknowledged_at"] is not None


async def test_ack_sets_fields_and_is_idempotent(app, client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    alice_id = await _user_id(client)
    await _add_membership(team_id, alice_id)

    response = await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    assert response.status_code == 200
    body = response.json()
    assert body["acknowledged_at"] is not None
    assert body["acknowledged_by"] == {"id": alice_id, "username": "alice"}

    async with db_module.async_session_factory() as session:
        audit_rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "alert.ack"))
        ).scalars().all()
        assert len(audit_rows) == 1

    # A second acker must not steal/overwrite the existing ack.
    async with await _fresh_client(app) as bob_client:
        await login_as(bob_client, username="bob")
        await _add_membership(team_id, await _user_id(bob_client))
        second = await bob_client.post(f"/api/v1/alerts/history/{event_id}/ack")
        assert second.status_code == 200
        assert second.json()["acknowledged_by"] == {"id": alice_id, "username": "alice"}

    async with db_module.async_session_factory() as session:
        audit_rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "alert.ack"))
        ).scalars().all()
        assert len(audit_rows) == 1  # no second audit row for the no-op idempotent call


async def test_unack_clears_fields(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    response = await client.delete(f"/api/v1/alerts/history/{event_id}/ack")
    assert response.status_code == 200
    body = response.json()
    assert body["acknowledged_at"] is None
    assert body["acknowledged_by"] is None


# -- assignee -----------------------------------------------------------


async def test_assignee_rejects_non_member_422(app, client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    async with await _fresh_client(app) as outsider_client:
        await login_as(outsider_client, username="dave")
        outsider_id = await _user_id(outsider_client)

    response = await client.put(
        f"/api/v1/alerts/history/{event_id}/assignee", json={"user_id": outsider_id}
    )
    assert response.status_code == 422


async def test_assignee_accepts_team_member(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    alice_id = await _user_id(client)
    await _add_membership(team_id, alice_id)

    response = await client.put(
        f"/api/v1/alerts/history/{event_id}/assignee", json={"user_id": alice_id}
    )
    assert response.status_code == 200
    assert response.json()["assignee"] == {"id": alice_id, "username": "alice"}

    # Clearing back to null is allowed.
    cleared = await client.put(
        f"/api/v1/alerts/history/{event_id}/assignee", json={"user_id": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["assignee"] is None


async def test_assignee_accepts_admin_account_even_if_not_team_member(
    app, client: AsyncClient
) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    async with await _fresh_client(app) as admin_client:
        await login_as(admin_client, username="admin-user", group_dns=[ADMIN_DN])
        admin_id = await _user_id(admin_client)

    response = await client.put(
        f"/api/v1/alerts/history/{event_id}/assignee", json={"user_id": admin_id}
    )
    assert response.status_code == 200
    assert response.json()["assignee"]["id"] == admin_id


async def test_history_list_reflects_ack_and_assignee(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    alice_id = await _user_id(client)
    await _add_membership(team_id, alice_id)
    await client.post(f"/api/v1/alerts/history/{event_id}/ack")
    await client.put(f"/api/v1/alerts/history/{event_id}/assignee", json={"user_id": alice_id})

    response = await client.get(f"/api/v1/alerts/history?team_id={team_id}")
    item = response.json()["items"][0]
    assert item["acknowledged_by"] == {"id": alice_id, "username": "alice"}
    assert item["assignee"] == {"id": alice_id, "username": "alice"}


# -- comments -------------------------------------------------------------


async def test_comment_blank_body_is_422(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/alerts/history/{event_id}/comments", json={"body": "   "}
    )
    assert response.status_code == 422


async def test_comment_create_and_list(client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    alice_id = await _user_id(client)
    await _add_membership(team_id, alice_id)

    create = await client.post(
        f"/api/v1/alerts/history/{event_id}/comments", json={"body": "  투입 완료  "}
    )
    assert create.status_code == 201
    body = create.json()
    assert body["body"] == "투입 완료"  # stripped
    assert body["user"] == {"id": alice_id, "username": "alice", "display_name": "Alice"}

    listing = await client.get(f"/api/v1/alerts/history/{event_id}/comments")
    assert listing.status_code == 200
    assert [c["body"] for c in listing.json()] == ["투입 완료"]


async def test_comment_delete_permission_matrix(app, client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    team_id = await _create_team("platform")
    event_id = await _create_event(cluster_id=cluster_id, fingerprint="f1", team_id=team_id)

    await login_as(client, username="alice")
    alice_id = await _user_id(client)
    await _add_membership(team_id, alice_id, role="member")
    comment_id = (
        await client.post(f"/api/v1/alerts/history/{event_id}/comments", json={"body": "hi"})
    ).json()["id"]

    # A different team member (not the author, not an owner) is forbidden.
    async with await _fresh_client(app) as bob_client:
        await login_as(bob_client, username="bob")
        await _add_membership(team_id, await _user_id(bob_client), role="member")
        forbidden = await bob_client.delete(f"/api/v1/comments/{comment_id}")
        assert forbidden.status_code == 403

    # The author can delete their own comment.
    own_delete = await client.delete(f"/api/v1/comments/{comment_id}")
    assert own_delete.status_code == 204

    # A team owner can delete someone else's comment.
    comment_id_2 = (
        await client.post(f"/api/v1/alerts/history/{event_id}/comments", json={"body": "hi2"})
    ).json()["id"]
    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="owner-user")
        await _add_membership(team_id, await _user_id(owner_client), role="owner")
        owner_delete = await owner_client.delete(f"/api/v1/comments/{comment_id_2}")
        assert owner_delete.status_code == 204

    # An admin can delete anything.
    comment_id_3 = (
        await client.post(f"/api/v1/alerts/history/{event_id}/comments", json={"body": "hi3"})
    ).json()["id"]
    async with await _fresh_client(app) as admin_client:
        await login_as(admin_client, username="admin-user", group_dns=[ADMIN_DN])
        admin_delete = await admin_client.delete(f"/api/v1/comments/{comment_id_3}")
        assert admin_delete.status_code == 204


# -- ack-status batch -------------------------------------------------------


async def test_ack_status_matches_and_scopes_by_team(app, client: AsyncClient) -> None:
    cluster_id = await _default_cluster_id()
    platform = await _create_team("platform")
    payments = await _create_team("payments")

    platform_event = await _create_event(
        cluster_id=cluster_id, fingerprint="fp-shared", team_id=platform
    )
    await _create_event(cluster_id=cluster_id, fingerprint="fp-other", team_id=payments)

    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        cluster_name = cluster.name

    await login_as(client, username="alice")
    alice_id = await _user_id(client)
    await _add_membership(platform, alice_id)
    await client.put(
        f"/api/v1/alerts/history/{platform_event}/assignee", json={"user_id": alice_id}
    )

    response = await client.post(
        f"/api/v1/alerts/ack-status?team_id={platform}",
        json={
            "items": [
                {"cluster": cluster_name, "fingerprint": "fp-shared"},
                {"cluster": cluster_name, "fingerprint": "fp-other"},
                {"cluster": cluster_name, "fingerprint": "does-not-exist"},
            ]
        },
    )
    assert response.status_code == 200
    matched = response.json()["matched"]
    # Only the requesting team's own fingerprint is returned -- the
    # `payments`-scoped one is silently dropped, not an error.
    assert [m["fingerprint"] for m in matched] == ["fp-shared"]
    assert matched[0]["acknowledged"] is False
    assert matched[0]["assignee_username"] == "alice"
    assert matched[0]["event_id"] == platform_event
