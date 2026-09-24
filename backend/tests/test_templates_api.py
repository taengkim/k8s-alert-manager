"""API tests for app/api/templates.py: RBAC, length/syntax validation,
usage-count reporting, delete-nulls-references behavior, preview (sample +
real event + cross-team 403), the variable reference list, and template_id
ownership checks on the channel/route APIs (Phase 13).
"""

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.routing import RoutingRule
from app.models.team import Team, TeamMembership
from app.models.template import MAX_TEMPLATE_LENGTH, MessageTemplate
from app.security import encrypt_str
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


async def _add_membership(team_id: int, user_id: int, role: str) -> None:
    async with db_module.async_session_factory() as session:
        session.add(TeamMembership(team_id=team_id, user_id=user_id, role=role, origin="manual"))
        await session.commit()


async def _create_template(team_id: int, name: str = "t1") -> int:
    async with db_module.async_session_factory() as session:
        template = MessageTemplate(
            team_id=team_id,
            name=name,
            title_template="[{{ severity | upper }}] {{ alertname }}",
            body_template="cluster: {{ cluster }}",
        )
        session.add(template)
        await session.commit()
        await session.refresh(template)
        return template.id


async def _create_channel(team_id: int, name: str = "email-1", template_id: int | None = None) -> int:
    async with db_module.async_session_factory() as session:
        channel = Channel(
            team_id=team_id,
            name=name,
            type="email",
            config_encrypted=encrypt_str('{"recipients": ["ops@example.org"]}'),
            template_id=template_id,
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel.id


async def _create_cluster(name: str = "tpl-cluster") -> int:
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


async def _create_event(team_id: int | None, cluster_id: int, *, fingerprint: str = "fp-1") -> int:
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


# -- CRUD RBAC ------------------------------------------------------------------


async def test_create_template_requires_owner(app) -> None:
    team_id = await _create_team("tpl-owner")

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        resp = await owner_client.post(
            f"/api/v1/teams/{team_id}/templates",
            json={
                "name": "custom-1",
                "title_template": "[{{ severity }}] {{ alertname }}",
                "body_template": "body",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "custom-1"
        assert body["channel_count"] == 0
        assert body["route_count"] == 0

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.post(
            f"/api/v1/teams/{team_id}/templates",
            json={"name": "x", "title_template": "x", "body_template": "x"},
        )
        assert resp.status_code == 403


async def test_list_templates_member_allowed_with_usage_counts(app) -> None:
    team_id = await _create_team("tpl-list")
    template_id = await _create_template(team_id)
    await _create_channel(team_id, template_id=template_id)

    async with await _fresh_client(app) as client:
        await login_as(client, username="dave")
        dave_id = (await client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, dave_id, "member")

        resp = await client.get(f"/api/v1/teams/{team_id}/templates")
        assert resp.status_code == 200
        [item] = resp.json()
        assert item["id"] == template_id
        assert item["channel_count"] == 1
        assert item["route_count"] == 0


async def test_get_template_non_member_403(app) -> None:
    team_id = await _create_team("tpl-get-priv")
    template_id = await _create_template(team_id)

    async with await _fresh_client(app) as outsider:
        await login_as(outsider, username="eve")
        resp = await outsider.get(f"/api/v1/templates/{template_id}")
        assert resp.status_code == 403


async def test_update_template_owner_only(app) -> None:
    team_id = await _create_team("tpl-update")
    template_id = await _create_template(team_id)

    async with await _fresh_client(app) as owner_client:
        await login_as(owner_client, username="bob")
        bob_id = (await owner_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        resp = await owner_client.put(
            f"/api/v1/templates/{template_id}",
            json={
                "name": "renamed",
                "title_template": "{{ alertname }}",
                "body_template": "updated body",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"
        assert resp.json()["body_template"] == "updated body"

    async with await _fresh_client(app) as member_client:
        await login_as(member_client, username="carol")
        carol_id = (await member_client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member_client.put(
            f"/api/v1/templates/{template_id}",
            json={"name": "hacked", "title_template": "x", "body_template": "x"},
        )
        assert resp.status_code == 403


# -- length caps + syntax validation --------------------------------------------


async def test_create_template_over_length_cap_422(client: AsyncClient) -> None:
    team_id = await _create_team("tpl-length")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/templates",
        json={
            "name": "too-long",
            "title_template": "x" * (MAX_TEMPLATE_LENGTH + 1),
            "body_template": "ok",
        },
    )
    assert resp.status_code == 422


async def test_create_template_syntax_error_422_with_position(client: AsyncClient) -> None:
    team_id = await _create_team("tpl-syntax")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/templates",
        json={
            "name": "broken",
            "title_template": "ok",
            "body_template": "line1\n{% if x %}",
        },
    )
    assert resp.status_code == 422
    [error] = resp.json()["detail"]
    assert error["slot"] == "body"
    assert error["lineno"] == 2


async def test_create_template_unsupported_kind_422(client: AsyncClient) -> None:
    """'report' is a supported kind as of Phase 20 (see
    test_create_template_report_kind below) -- this now exercises a kind
    that's still genuinely unsupported.
    """
    team_id = await _create_team("tpl-kind")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/templates",
        json={
            "name": "bogus-kind",
            "kind": "bogus",
            "title_template": "ok",
            "body_template": "ok",
        },
    )
    assert resp.status_code == 422


async def test_create_template_report_kind(client: AsyncClient) -> None:
    """Phase 20: 'report' is a valid kind alongside 'alert'."""
    team_id = await _create_team("tpl-report-kind")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/templates",
        json={
            "name": "weekly-report",
            "kind": "report",
            "title_template": "[KAM] {{ team }} report",
            "body_template": "events: {{ summary.events_in_range }}",
        },
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "report"


async def test_create_template_duplicate_name_409(client: AsyncClient) -> None:
    team_id = await _create_team("tpl-dup")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    body = {"name": "dup", "title_template": "ok", "body_template": "ok"}

    first = await client.post(f"/api/v1/teams/{team_id}/templates", json=body)
    assert first.status_code == 201
    second = await client.post(f"/api/v1/teams/{team_id}/templates", json=body)
    assert second.status_code == 409


# -- delete nulls references ----------------------------------------------------


async def test_delete_template_nulls_channel_and_route_references(client: AsyncClient) -> None:
    team_id = await _create_team("tpl-delete")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    template_id = await _create_template(team_id)
    channel_id = await _create_channel(team_id, template_id=template_id)

    async with db_module.async_session_factory() as session:
        rule = RoutingRule(
            team_id=team_id,
            name="uses-template",
            action="notify",
            template_id=template_id,
            channels=[await session.get(Channel, channel_id)],
        )
        session.add(rule)
        await session.commit()
        rule_id = rule.id

    resp = await client.delete(f"/api/v1/templates/{template_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["unassigned_channels"] == 1
    assert body["unassigned_routes"] == 1
    assert "detail" in body

    async with db_module.async_session_factory() as session:
        channel = await session.get(Channel, channel_id)
        rule = await session.get(RoutingRule, rule_id)
        assert channel.template_id is None
        assert rule.template_id is None
        assert await session.get(MessageTemplate, template_id) is None


# -- preview ---------------------------------------------------------------------


async def test_preview_uses_sample_when_no_event_given(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        "/api/v1/templates/preview",
        json={
            "title_template": "[{{ severity | upper }}] {{ alertname }}",
            "body_template": "cluster: {{ cluster }}",
            "use_sample": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendered"]["title"] == "[WARNING] KamTestAlert"
    assert body["errors"] == []


async def test_preview_syntax_error_reports_position(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        "/api/v1/templates/preview",
        json={"title_template": "{% if x %}", "body_template": "ok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rendered"] is None
    assert body["errors"][0]["slot"] == "title"
    assert body["errors"][0]["lineno"] == 1


async def test_preview_undefined_variable_is_a_warning_not_an_error(client: AsyncClient) -> None:
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        "/api/v1/templates/preview",
        json={"title_template": "{{ alertname }}: {{ typo_var }}", "body_template": "ok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["errors"] == []
    assert body["warnings"] == ["typo_var"]
    assert body["rendered"] is not None


async def test_preview_with_real_event_uses_its_data(client: AsyncClient) -> None:
    team_id = await _create_team("tpl-preview-event")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    cluster_id = await _create_cluster()
    event_id = await _create_event(team_id, cluster_id, fingerprint="preview-fp-1")

    resp = await client.post(
        "/api/v1/templates/preview",
        json={
            "title_template": "{{ alertname }}",
            "body_template": "ok",
            "alert_event_id": event_id,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["rendered"]["title"] == "HighCpu"


async def test_preview_with_other_team_event_403(app) -> None:
    team_a = await _create_team("tpl-preview-a")
    team_b = await _create_team("tpl-preview-b")
    cluster_id = await _create_cluster("tpl-preview-cluster")
    event_id = await _create_event(team_a, cluster_id, fingerprint="preview-fp-2")

    async with await _fresh_client(app) as client:
        await login_as(client, username="frank")
        frank_id = (await client.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_b, frank_id, "member")

        resp = await client.post(
            "/api/v1/templates/preview",
            json={
                "title_template": "{{ alertname }}",
                "body_template": "ok",
                "alert_event_id": event_id,
            },
        )
        assert resp.status_code == 403


async def test_preview_by_zero_team_user_403(client: AsyncClient) -> None:
    """A user belonging to no team at all has no legitimate reason to drive
    template compilation (each call queues onto the shared, capacity-limited
    render pool -- see templating.py's module docstring) -- gated even for
    the sample-alert path, which has no team_id of its own to check.
    """
    await login_as(client, username="ghost")  # no team, no membership, not admin

    resp = await client.post(
        "/api/v1/templates/preview",
        json={"title_template": "{{ alertname }}", "body_template": "ok", "use_sample": True},
    )
    assert resp.status_code == 403


async def test_template_variables_endpoint(client: AsyncClient) -> None:
    await login_as(client, username="alice")
    resp = await client.get("/api/v1/templates/variables")
    assert resp.status_code == 200
    names = {v["name"] for v in resp.json()}
    assert "alertname" in names
    assert "now()" in names


# -- template_id ownership on channels/routes -----------------------------------


async def test_create_channel_with_template_from_other_team_422(client: AsyncClient) -> None:
    team_a = await _create_team("tpl-chan-a")
    team_b = await _create_team("tpl-chan-b")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    other_team_template_id = await _create_template(team_b)

    resp = await client.post(
        f"/api/v1/teams/{team_a}/channels",
        json={
            "name": "c1",
            "type": "email",
            "config": {"recipients": ["ops@example.org"]},
            "template_id": other_team_template_id,
        },
    )
    assert resp.status_code == 422


async def test_patch_channel_template_id_same_team_ok_and_clearable(client: AsyncClient) -> None:
    team_id = await _create_team("tpl-chan-patch")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    template_id = await _create_template(team_id)
    channel_id = await _create_channel(team_id)

    resp = await client.patch(
        f"/api/v1/channels/{channel_id}", json={"template_id": template_id}
    )
    assert resp.status_code == 200
    assert resp.json()["template_id"] == template_id

    # Explicit null clears it back to "use the channel type's default".
    resp = await client.patch(f"/api/v1/channels/{channel_id}", json={"template_id": None})
    assert resp.status_code == 200
    assert resp.json()["template_id"] is None


async def test_create_route_with_template_from_other_team_422(client: AsyncClient) -> None:
    team_a = await _create_team("tpl-route-a")
    team_b = await _create_team("tpl-route-b")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    other_team_template_id = await _create_template(team_b)
    channel_id = await _create_channel(team_a)

    resp = await client.post(
        f"/api/v1/teams/{team_a}/routes",
        json={
            "name": "r1",
            "action": "notify",
            "channel_ids": [channel_id],
            "template_id": other_team_template_id,
        },
    )
    assert resp.status_code == 422


async def test_create_route_with_own_team_template_ok(client: AsyncClient) -> None:
    team_id = await _create_team("tpl-route-ok")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    template_id = await _create_template(team_id)
    channel_id = await _create_channel(team_id)

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json={
            "name": "r1",
            "action": "notify",
            "channel_ids": [channel_id],
            "template_id": template_id,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["template_id"] == template_id
