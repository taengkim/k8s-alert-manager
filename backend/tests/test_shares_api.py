"""API tests for the Phase 14 AlertShare CRUD endpoints: outgoing/incoming
listing, create/update/delete RBAC (owner-only, admin bypass), self-share
422, duplicate 409, and GET /teams/all-brief.
"""

from httpx import AsyncClient

import app.db as db_module
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _create_team(slug: str) -> Team:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team


async def _add_membership(client: AsyncClient, team_id: int, role: str = "member") -> None:
    me = (await client.get("/api/v1/auth/me")).json()
    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(team_id=team_id, user_id=me["id"], role=role, origin="manual")
        )
        await session.commit()


# -- all-brief ----------------------------------------------------------


async def test_all_brief_requires_auth(client: AsyncClient) -> None:
    response = await client.get("/api/v1/teams/all-brief")
    assert response.status_code == 401


async def test_all_brief_lists_every_team_regardless_of_membership(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="carol")  # no memberships at all

    response = await client.get("/api/v1/teams/all-brief")
    assert response.status_code == 200
    slugs = {t["slug"] for t in response.json()}
    assert {"platform", "payments"} <= slugs
    assert set(response.json()[0].keys()) == {"id", "slug", "name"}
    # sanity: ids actually correspond to the created teams
    by_slug = {t["slug"]: t["id"] for t in response.json()}
    assert by_slug["platform"] == platform.id
    assert by_slug["payments"] == payments.id


# -- create ---------------------------------------------------------------


async def test_create_share_requires_owner(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="member")

    response = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view"},
    )
    assert response.status_code == 403


async def test_owner_can_create_view_notify_share(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")

    response = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view_notify"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["owner_team_id"] == platform.id
    assert body["target_team_id"] == payments.id
    assert body["target_team_slug"] == "payments"
    assert body["mode"] == "view_notify"
    assert body["matchers"] is None


async def test_admin_can_create_share_without_membership(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view"},
    )
    assert response.status_code == 201


async def test_self_share_is_422(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")

    response = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": platform.id, "mode": "view"},
    )
    assert response.status_code == 422


async def test_unknown_target_team_is_422(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")

    response = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": 999999, "mode": "view"},
    )
    assert response.status_code == 422


async def test_duplicate_owner_target_pair_is_409(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")

    first = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view"},
    )
    assert first.status_code == 201

    second = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view_notify"},
    )
    assert second.status_code == 409


async def test_invalid_matcher_pattern_is_422(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")

    response = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={
            "target_team_id": payments.id,
            "mode": "view",
            "matchers": [{"kind": "include", "target": "alertname", "pattern": "(unterminated"}],
        },
    )
    assert response.status_code == 422


async def test_matcher_missing_key_for_label_target_is_422(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")

    response = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={
            "target_team_id": payments.id,
            "mode": "view",
            "matchers": [{"kind": "include", "target": "label", "pattern": "x"}],
        },
    )
    assert response.status_code == 422


# -- listing ---------------------------------------------------------------


async def test_list_outgoing_shares(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")
    await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view"},
    )

    response = await client.get(f"/api/v1/teams/{platform.id}/shares")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["target_team_slug"] == "payments"


async def test_list_outgoing_shares_requires_membership(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    await login_as(client, username="carol")

    response = await client.get(f"/api/v1/teams/{platform.id}/shares")
    assert response.status_code == 403


async def test_list_incoming_shares(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    async with db_module.async_session_factory() as session:
        from app.models.share import AlertShare

        session.add(
            AlertShare(owner_team_id=platform.id, target_team_id=payments.id, mode="view_notify")
        )
        await session.commit()

    await login_as(client, username="alice")
    await _add_membership(client, payments.id, role="member")

    response = await client.get(f"/api/v1/teams/{payments.id}/shared-with-me")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["owner_team_slug"] == "platform"
    assert body[0]["mode"] == "view_notify"


# -- update / delete --------------------------------------------------------


async def test_update_share_requires_owner_of_owner_team(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    async with db_module.async_session_factory() as session:
        from app.models.share import AlertShare

        share = AlertShare(owner_team_id=platform.id, target_team_id=payments.id, mode="view")
        session.add(share)
        await session.commit()
        await session.refresh(share)
        share_id = share.id

    # payments is the target, not the owner -- its membership (even owner)
    # doesn't grant edit rights on platform's outgoing share.
    await login_as(client, username="alice")
    await _add_membership(client, payments.id, role="owner")

    response = await client.put(f"/api/v1/shares/{share_id}", json={"mode": "view_notify"})
    assert response.status_code == 403


async def test_owner_can_update_share_mode_and_matchers(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")
    created = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view"},
    )
    share_id = created.json()["id"]

    response = await client.put(
        f"/api/v1/shares/{share_id}",
        json={
            "mode": "view_notify",
            "matchers": [{"kind": "include", "target": "alertname", "pattern": "^Foo$"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "view_notify"
    assert body["matchers"] == [{"kind": "include", "target": "alertname", "pattern": "^Foo$"}]


async def test_update_with_omitted_fields_leaves_them_unchanged(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")
    created = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={
            "target_team_id": payments.id,
            "mode": "view_notify",
            "matchers": [{"kind": "include", "target": "alertname", "pattern": "^Foo$"}],
        },
    )
    share_id = created.json()["id"]

    # Body has neither key -- both mode and matchers must survive untouched.
    response = await client.put(f"/api/v1/shares/{share_id}", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "view_notify"
    assert body["matchers"] == [{"kind": "include", "target": "alertname", "pattern": "^Foo$"}]


async def test_update_matchers_to_null_clears_scope(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")
    created = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={
            "target_team_id": payments.id,
            "mode": "view",
            "matchers": [{"kind": "include", "target": "alertname", "pattern": "^Foo$"}],
        },
    )
    share_id = created.json()["id"]

    response = await client.put(f"/api/v1/shares/{share_id}", json={"matchers": None})
    assert response.status_code == 200
    assert response.json()["matchers"] is None


async def test_delete_share_requires_owner(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="member")
    async with db_module.async_session_factory() as session:
        from app.models.share import AlertShare

        share = AlertShare(owner_team_id=platform.id, target_team_id=payments.id, mode="view")
        session.add(share)
        await session.commit()
        await session.refresh(share)
        share_id = share.id

    response = await client.delete(f"/api/v1/shares/{share_id}")
    assert response.status_code == 403


async def test_owner_can_delete_share(client: AsyncClient) -> None:
    platform = await _create_team("platform")
    payments = await _create_team("payments")
    await login_as(client, username="alice")
    await _add_membership(client, platform.id, role="owner")
    created = await client.post(
        f"/api/v1/teams/{platform.id}/shares",
        json={"target_team_id": payments.id, "mode": "view"},
    )
    share_id = created.json()["id"]

    response = await client.delete(f"/api/v1/shares/{share_id}")
    assert response.status_code == 204

    listing = await client.get(f"/api/v1/teams/{platform.id}/shares")
    assert listing.json() == []
