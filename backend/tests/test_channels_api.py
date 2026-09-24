"""API tests for channel CRUD + test-send (app/api/channels.py): RBAC,
config schema validation, Fernet-encrypted storage, and the test-send
endpoint's success/failure mapping.
"""

from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.audit import AuditLog
from app.models.channel import Channel
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


async def _fresh_client(app) -> AsyncClient:
    """A second AsyncClient on the same app/db with its own cookie jar."""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _create_team(name: str) -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=name, name=name.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(team_id: int, user_id: int, role: str) -> None:
    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(team_id=team_id, user_id=user_id, role=role, origin="manual")
        )
        await session.commit()


async def test_channel_types_lists_builtin_email(client: AsyncClient) -> None:
    await login_as(client, username="alice")
    resp = await client.get("/api/v1/channel-types")
    assert resp.status_code == 200

    types = resp.json()
    email_type = next(item for item in types if item["type_name"] == "email")
    assert email_type["display_name"] == "Email"
    assert "recipients" in email_type["json_schema"]["properties"]


async def test_create_channel_requires_owner(app) -> None:
    team_id = await _create_team("payments")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={
                "name": "ops-email",
                "type": "email",
                "config": {"recipients": ["ops@example.org"]},
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"] == "email"
        assert body["enabled"] is True
        assert body["config"]["recipients"] == ["ops@example.org"]

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "other", "type": "email", "config": {"recipients": ["x@example.org"]}},
        )
        assert resp.status_code == 403

    async with await _fresh_client(app) as outsider_client:
        await login_as(outsider_client, username="dave")
        resp = await outsider_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "other2", "type": "email", "config": {"recipients": ["x@example.org"]}},
        )
        assert resp.status_code == 403


async def test_admin_bypasses_team_rbac(client: AsyncClient) -> None:
    team_id = await _create_team("admin-owned")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "admin-email", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    assert resp.status_code == 201


async def test_create_channel_unknown_type_404(client: AsyncClient) -> None:
    team_id = await _create_team("t-unknown")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "x", "type": "sms", "config": {}},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown channel type"


async def test_create_channel_invalid_config_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-invalid")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "bad", "type": "email", "config": {"recipients": []}},
    )
    assert resp.status_code == 422
    assert isinstance(resp.json()["detail"], list)


async def test_create_channel_encrypts_config_and_get_round_trips(client: AsyncClient) -> None:
    team_id = await _create_team("t-crypt")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={
            "name": "secret-email",
            "type": "email",
            "config": {"recipients": ["ops@example.org"]},
        },
    )
    channel_id = create_resp.json()["id"]

    async with db_module.async_session_factory() as session:
        row = await session.get(Channel, channel_id)
        assert "ops@example.org" not in row.config_encrypted
        assert "ops" not in row.config_encrypted

    get_resp = await client.get(f"/api/v1/teams/{team_id}/channels")
    assert get_resp.status_code == 200
    [item] = [c for c in get_resp.json() if c["id"] == channel_id]
    assert item["config"]["recipients"] == ["ops@example.org"]
    assert item["config"]["subject_prefix"] == "[KAM]"


async def test_patch_channel_owner_only_and_updates_fields(app) -> None:
    team_id = await _create_team("t-patch")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "c1", "type": "email", "config": {"recipients": ["a@example.org"]}},
        )
        channel_id = create_resp.json()["id"]

        patch_resp = await owner_client.patch(
            f"/api/v1/channels/{channel_id}",
            json={
                "name": "c1-renamed",
                "enabled": False,
                "config": {"recipients": ["b@example.org"]},
            },
        )
        assert patch_resp.status_code == 200
        body = patch_resp.json()
        assert body["name"] == "c1-renamed"
        assert body["enabled"] is False
        assert body["config"]["recipients"] == ["b@example.org"]

        bad_patch = await owner_client.patch(
            f"/api/v1/channels/{channel_id}", json={"config": {"recipients": []}}
        )
        assert bad_patch.status_code == 422

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.patch(
            f"/api/v1/channels/{channel_id}", json={"name": "hacked"}
        )
        assert resp.status_code == 403


async def test_delete_channel_owner_only(app) -> None:
    team_id = await _create_team("t-delete")
    channel_id: int

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={
                "name": "to-delete",
                "type": "email",
                "config": {"recipients": ["a@example.org"]},
            },
        )
        channel_id = create_resp.json()["id"]

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.delete(f"/api/v1/channels/{channel_id}")
        assert resp.status_code == 403

    async with await _fresh_client(app) as owner_client2:
        await login_as(owner_client2, username="bob")
        resp = await owner_client2.delete(f"/api/v1/channels/{channel_id}")
        assert resp.status_code == 204

    async with db_module.async_session_factory() as session:
        # Soft-deleted, not removed: the row (and its delivery history)
        # must survive -- see test_channel_soft_delete.py for the fuller
        # soft-delete behavior (list exclusion, name reuse, etc).
        channel = await session.get(Channel, channel_id)
        assert channel is not None
        assert channel.deleted_at is not None


async def test_test_endpoint_success_202_and_audit_row(client: AsyncClient) -> None:
    team_id = await _create_team("t-test-ok")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "test-ch", "type": "email", "config": {"recipients": ["ops@example.org"]}},
    )
    channel_id = create_resp.json()["id"]

    mock_send = AsyncMock(return_value=({}, "OK"))
    with patch("app.channels.email.aiosmtplib.send", new=mock_send):
        resp = await client.post(f"/api/v1/channels/{channel_id}/test")
    assert resp.status_code == 202
    assert mock_send.await_count == 1

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "channel.test"))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].team_id == team_id
        assert rows[0].object_ref == "test-ch"


async def test_test_endpoint_member_allowed_but_failure_is_502_no_audit(app) -> None:
    team_id = await _create_team("t-test-fail")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={
                "name": "will-fail",
                "type": "email",
                "config": {"recipients": ["ops@example.org"]},
            },
        )
        channel_id = create_resp.json()["id"]

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        mock_send = AsyncMock(side_effect=OSError("connection refused"))
        with patch("app.channels.email.aiosmtplib.send", new=mock_send):
            resp = await member_client.post(f"/api/v1/channels/{channel_id}/test")
        assert resp.status_code == 502
        assert "connection refused" in resp.json()["detail"]

    async with db_module.async_session_factory() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "channel.test"))
        ).scalars().all()
        assert rows == []


async def test_test_endpoint_non_member_403(app) -> None:
    team_id = await _create_team("t-test-outsider")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/channels",
            json={"name": "priv", "type": "email", "config": {"recipients": ["ops@example.org"]}},
        )
        channel_id = create_resp.json()["id"]

    async with await _fresh_client(app) as outsider_client:
        await login_as(outsider_client, username="dave")
        resp = await outsider_client.post(f"/api/v1/channels/{channel_id}/test")
        assert resp.status_code == 403


# -- Phase 15: allow_cross_team_escalation + escalation-targets --------------


async def test_create_channel_defaults_allow_cross_team_escalation_false(
    client: AsyncClient,
) -> None:
    team_id = await _create_team("t-esc-default")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "c1", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    assert resp.status_code == 201
    assert resp.json()["allow_cross_team_escalation"] is False


async def test_patch_channel_toggles_allow_cross_team_escalation(client: AsyncClient) -> None:
    team_id = await _create_team("t-esc-toggle")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "c1", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    channel_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/api/v1/channels/{channel_id}", json={"allow_cross_team_escalation": True}
    )
    assert resp.status_code == 200
    assert resp.json()["allow_cross_team_escalation"] is True


async def test_escalation_targets_includes_own_team_and_opted_in_other_teams(
    client: AsyncClient,
) -> None:
    team_a = await _create_team("t-targets-a")
    team_b = await _create_team("t-targets-b")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    own_resp = await client.post(
        f"/api/v1/teams/{team_a}/channels",
        json={"name": "own-channel", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    own_id = own_resp.json()["id"]

    opted_in_resp = await client.post(
        f"/api/v1/teams/{team_b}/channels",
        json={
            "name": "opted-in",
            "type": "email",
            "config": {"recipients": ["b@example.org"]},
            "allow_cross_team_escalation": True,
        },
    )
    opted_in_id = opted_in_resp.json()["id"]

    not_opted_in_resp = await client.post(
        f"/api/v1/teams/{team_b}/channels",
        json={"name": "not-opted-in", "type": "email", "config": {"recipients": ["c@example.org"]}},
    )
    not_opted_in_id = not_opted_in_resp.json()["id"]

    resp = await client.get(f"/api/v1/channels/escalation-targets?team_id={team_a}")
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()}
    assert own_id in ids
    assert opted_in_id in ids
    assert not_opted_in_id not in ids

    by_id = {item["id"]: item for item in resp.json()}
    assert by_id[opted_in_id]["team_slug"] == "t-targets-b"


async def test_escalation_targets_requires_team_membership(app) -> None:
    team_id = await _create_team("t-targets-forbidden")

    async with await _fresh_client(app) as outsider_client:
        await login_as(outsider_client, username="dave")
        resp = await outsider_client.get(f"/api/v1/channels/escalation-targets?team_id={team_id}")
        assert resp.status_code == 403


# -- I1: deleting the last escalation channel must not lock the rule out ----


async def test_delete_last_escalation_channel_clears_escalation_and_unlocks_save(
    client: AsyncClient,
) -> None:
    team_id = await _create_team("t-esc-lockout")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    primary_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "primary", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    primary_id = primary_resp.json()["id"]
    esc_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "esc", "type": "email", "config": {"recipients": ["b@example.org"]}},
    )
    esc_id = esc_resp.json()["id"]

    route_resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json={
            "name": "esc-rule",
            "action": "notify",
            "enabled": True,
            "notify_on_firing": True,
            "notify_on_resolved": False,
            "include_shared": False,
            "channel_ids": [primary_id],
            "matchers": [],
            "escalation_enabled": True,
            "escalation_after_minutes": 5,
            "escalation_channel_ids": [esc_id],
        },
    )
    assert route_resp.status_code == 201
    route = route_resp.json()

    delete_resp = await client.delete(f"/api/v1/channels/{esc_id}")
    assert delete_resp.status_code == 204

    get_resp = await client.get(f"/api/v1/routes/{route['id']}")
    reloaded = get_resp.json()
    assert reloaded["escalation_enabled"] is False
    assert reloaded["escalation_after_minutes"] is None
    assert reloaded["escalation_channel_ids"] == []

    # The exact bug this guards: PUT-ing the rule back unchanged (e.g. the
    # Routes list page's enable/disable toggle) must not 422.
    toggle_resp = await client.put(
        f"/api/v1/routes/{route['id']}",
        json={
            "name": reloaded["name"],
            "action": reloaded["action"],
            "enabled": reloaded["enabled"],
            "notify_on_firing": reloaded["notify_on_firing"],
            "notify_on_resolved": reloaded["notify_on_resolved"],
            "include_shared": reloaded["include_shared"],
            "channel_ids": reloaded["channel_ids"],
            "matchers": [],
            "escalation_enabled": reloaded["escalation_enabled"],
            "escalation_channel_ids": reloaded["escalation_channel_ids"],
        },
    )
    assert toggle_resp.status_code == 200

    async with db_module.async_session_factory() as session:
        audit_rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "channel.delete"))
        ).scalars().all()
        assert len(audit_rows) == 1
        assert audit_rows[0].detail == {"escalation_disabled_rule_ids": [route["id"]]}


# -- I2: revoking allow_cross_team_escalation removes foreign joins ---------


async def test_revoking_cross_team_flag_strips_foreign_joins_and_keeps_own_team(
    client: AsyncClient,
) -> None:
    owner_team_id = await _create_team("t-i2-owner")
    other_team_id = await _create_team("t-i2-other")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    channel_resp = await client.post(
        f"/api/v1/teams/{owner_team_id}/channels",
        json={
            "name": "shared-esc",
            "type": "email",
            "config": {"recipients": ["a@example.org"]},
            "allow_cross_team_escalation": True,
        },
    )
    channel_id = channel_resp.json()["id"]

    own_channel_resp = await client.post(
        f"/api/v1/teams/{owner_team_id}/channels",
        json={"name": "own-primary", "type": "email", "config": {"recipients": ["b@example.org"]}},
    )
    own_channel_id = own_channel_resp.json()["id"]

    other_channel_resp = await client.post(
        f"/api/v1/teams/{other_team_id}/channels",
        json={"name": "other-primary", "type": "email", "config": {"recipients": ["c@example.org"]}},
    )
    other_channel_id = other_channel_resp.json()["id"]

    def _rule_body(channel_ids: list[int], escalation_channel_ids: list[int]) -> dict:
        return {
            "name": "rule",
            "action": "notify",
            "enabled": True,
            "notify_on_firing": True,
            "notify_on_resolved": False,
            "include_shared": False,
            "channel_ids": channel_ids,
            "matchers": [],
            "escalation_enabled": True,
            "escalation_after_minutes": 5,
            "escalation_channel_ids": escalation_channel_ids,
        }

    # A rule in the OTHER (foreign) team, selecting the shared channel only
    # because it opted in to cross-team escalation.
    foreign_rule_resp = await client.post(
        f"/api/v1/teams/{other_team_id}/routes",
        json=_rule_body([other_channel_id], [channel_id]),
    )
    assert foreign_rule_resp.status_code == 201
    foreign_rule = foreign_rule_resp.json()

    # A rule in the channel's OWN team, also selecting it -- same-team
    # selection never depended on allow_cross_team_escalation and must
    # survive the flag flipping off.
    own_rule_resp = await client.post(
        f"/api/v1/teams/{owner_team_id}/routes",
        json=_rule_body([own_channel_id], [channel_id]),
    )
    assert own_rule_resp.status_code == 201
    own_rule = own_rule_resp.json()

    patch_resp = await client.patch(
        f"/api/v1/channels/{channel_id}", json={"allow_cross_team_escalation": False}
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["allow_cross_team_escalation"] is False

    foreign_reloaded = (await client.get(f"/api/v1/routes/{foreign_rule['id']}")).json()
    assert foreign_reloaded["escalation_enabled"] is False
    assert foreign_reloaded["escalation_channel_ids"] == []

    own_reloaded = (await client.get(f"/api/v1/routes/{own_rule['id']}")).json()
    assert own_reloaded["escalation_enabled"] is True
    assert own_reloaded["escalation_channel_ids"] == [channel_id]

    async with db_module.async_session_factory() as session:
        audit_rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "channel.update"))
        ).scalars().all()
        assert len(audit_rows) == 1
        assert audit_rows[0].detail == {"escalation_disabled_rule_ids": [foreign_rule["id"]]}


# -- Phase 16: storm control (rate_limit_per_hour / digest_mode) fields ------


async def test_create_channel_defaults_digest_off(client: AsyncClient) -> None:
    team_id = await _create_team("t-digest-default")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "c1", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["digest_mode"] == "off"
    assert body["rate_limit_per_hour"] is None
    assert body["digest_window_minutes"] == 5


async def test_create_channel_with_storm_control_fields(client: AsyncClient) -> None:
    team_id = await _create_team("t-digest-create")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={
            "name": "c1",
            "type": "email",
            "config": {"recipients": ["a@example.org"]},
            "rate_limit_per_hour": 10,
            "digest_mode": "auto",
            "digest_window_minutes": 15,
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["rate_limit_per_hour"] == 10
    assert body["digest_mode"] == "auto"
    assert body["digest_window_minutes"] == 15


async def test_create_channel_auto_mode_without_rate_limit_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-digest-invalid")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={
            "name": "c1",
            "type": "email",
            "config": {"recipients": ["a@example.org"]},
            "digest_mode": "auto",
        },
    )
    assert resp.status_code == 422


async def test_patch_channel_updates_storm_control_fields(client: AsyncClient) -> None:
    team_id = await _create_team("t-digest-patch")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "c1", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    channel_id = create_resp.json()["id"]

    patch_resp = await client.patch(
        f"/api/v1/channels/{channel_id}",
        json={"rate_limit_per_hour": 5, "digest_mode": "auto", "digest_window_minutes": 10},
    )
    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["rate_limit_per_hour"] == 5
    assert body["digest_mode"] == "auto"
    assert body["digest_window_minutes"] == 10


async def test_patch_channel_to_auto_without_existing_rate_limit_422(client: AsyncClient) -> None:
    """Setting digest_mode='auto' alone, on a channel with no
    rate_limit_per_hour from before, must 422 -- the validation looks at the
    channel's FINAL merged state, not just the fields this one PATCH body
    happens to touch.
    """
    team_id = await _create_team("t-digest-patch-invalid")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={"name": "c1", "type": "email", "config": {"recipients": ["a@example.org"]}},
    )
    channel_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/v1/channels/{channel_id}", json={"digest_mode": "auto"})
    assert resp.status_code == 422


async def test_patch_channel_rate_limit_alone_does_not_422_when_already_auto(
    client: AsyncClient,
) -> None:
    """The inverse of the above: a PATCH that only sets rate_limit_per_hour
    must succeed when digest_mode was already 'auto' from a prior request,
    even though this body alone doesn't mention digest_mode.
    """
    team_id = await _create_team("t-digest-patch-valid")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={
            "name": "c1",
            "type": "email",
            "config": {"recipients": ["a@example.org"]},
            "rate_limit_per_hour": 3,
            "digest_mode": "auto",
        },
    )
    channel_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/v1/channels/{channel_id}", json={"rate_limit_per_hour": 7})
    assert resp.status_code == 200
    assert resp.json()["rate_limit_per_hour"] == 7
    assert resp.json()["digest_mode"] == "auto"


async def test_patch_channel_can_clear_rate_limit_back_to_unlimited(client: AsyncClient) -> None:
    team_id = await _create_team("t-digest-clear")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/channels",
        json={
            "name": "c1",
            "type": "email",
            "config": {"recipients": ["a@example.org"]},
            "rate_limit_per_hour": 3,
            "digest_mode": "off",
        },
    )
    channel_id = create_resp.json()["id"]

    resp = await client.patch(f"/api/v1/channels/{channel_id}", json={"rate_limit_per_hour": None})
    assert resp.status_code == 200
    assert resp.json()["rate_limit_per_hour"] is None
