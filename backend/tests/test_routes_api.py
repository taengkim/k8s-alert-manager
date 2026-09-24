"""API tests for app/api/routes.py: RBAC, server-side validation, and the
draft-rule preview endpoint's verdict accuracy.
"""

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.routing import RoutingRule
from app.models.team import Team, TeamMembership
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
        session.add(
            TeamMembership(team_id=team_id, user_id=user_id, role=role, origin="manual")
        )
        await session.commit()


async def _create_channel(
    team_id: int, name: str = "email-1", *, allow_cross_team_escalation: bool = False
) -> int:
    async with db_module.async_session_factory() as session:
        channel = Channel(
            team_id=team_id,
            name=name,
            type="email",
            config_encrypted=encrypt_str('{"recipients": ["ops@example.org"]}'),
            allow_cross_team_escalation=allow_cross_team_escalation,
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel.id


async def _create_cluster(name: str = "rt-cluster") -> int:
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


async def _create_event(
    team_id: int, cluster_id: int, *, alertname: str = "HighCpu", severity: str = "critical",
    fingerprint: str = "fp-1", status: str = "firing",
) -> int:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        event = AlertEvent(
            cluster_id=cluster_id,
            cluster_name=cluster.name,
            fingerprint=fingerprint,
            status=status,
            alertname=alertname,
            severity=severity,
            namespace="kam-demo",
            labels={"alertname": alertname},
            annotations={},
            team_id=team_id,
            starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event.id


def _notify_body(**overrides) -> dict:
    body = {
        "name": "notify-rule",
        "action": "notify",
        "channel_ids": [],
        "matchers": [],
    }
    body.update(overrides)
    return body


async def test_list_and_create_require_member_and_owner(app) -> None:
    team_id = await _create_team("t-rbac")
    channel_id = await _create_channel(team_id)

    async with await _fresh_client(app) as outsider:
        await login_as(outsider, username="dave")
        resp = await outsider.get(f"/api/v1/teams/{team_id}/routes")
        assert resp.status_code == 403

    async with await _fresh_client(app) as member:
        await login_as(member, username="carol")
        carol_id = (await member.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")

        resp = await member.get(f"/api/v1/teams/{team_id}/routes")
        assert resp.status_code == 200

        create_resp = await member.post(
            f"/api/v1/teams/{team_id}/routes",
            json=_notify_body(channel_ids=[channel_id]),
        )
        assert create_resp.status_code == 403

    async with await _fresh_client(app) as owner:
        await login_as(owner, username="bob")
        bob_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")

        create_resp = await owner.post(
            f"/api/v1/teams/{team_id}/routes",
            json=_notify_body(channel_ids=[channel_id]),
        )
        assert create_resp.status_code == 201
        body = create_resp.json()
        assert body["action"] == "notify"
        assert body["channel_ids"] == [channel_id]
        assert body["enabled"] is True
        assert body["notify_on_firing"] is True
        assert body["notify_on_resolved"] is False


async def test_create_rejects_invalid_matcher_pattern_with_position(client: AsyncClient) -> None:
    team_id = await _create_team("t-badpattern")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[await _create_channel(team_id)],
            matchers=[{"kind": "include", "target": "alertname", "pattern": "(unterminated"}],
        ),
    )
    assert resp.status_code == 422
    assert "matchers[0]" in resp.json()["detail"]


async def test_create_rejects_pattern_over_512_chars(client: AsyncClient) -> None:
    team_id = await _create_team("t-toolong")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[await _create_channel(team_id)],
            matchers=[{"kind": "include", "target": "alertname", "pattern": "a" * 513}],
        ),
    )
    assert resp.status_code == 422
    assert "matchers[0]" in resp.json()["detail"]


async def test_create_rejects_label_matcher_without_key(client: AsyncClient) -> None:
    team_id = await _create_team("t-nokey")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[await _create_channel(team_id)],
            matchers=[{"kind": "include", "target": "label", "pattern": "x"}],
        ),
    )
    assert resp.status_code == 422


async def test_create_rejects_invalid_severity(client: AsyncClient) -> None:
    team_id = await _create_team("t-badsev")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[await _create_channel(team_id)], severities=["urgent"]
        ),
    )
    assert resp.status_code == 422
    assert "urgent" in resp.json()["detail"]


async def test_suppress_rule_with_channels_is_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-suppress-chan")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(action="suppress", channel_ids=[await _create_channel(team_id)]),
    )
    assert resp.status_code == 422


async def test_notify_rule_without_channels_is_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-nochan")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes", json=_notify_body(channel_ids=[])
    )
    assert resp.status_code == 422


async def test_suppress_rule_without_channels_succeeds(client: AsyncClient) -> None:
    team_id = await _create_team("t-suppress-ok")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(name="suppress-rule", action="suppress", channel_ids=[]),
    )
    assert resp.status_code == 201
    assert resp.json()["channel_ids"] == []


async def test_channel_from_other_team_is_422(client: AsyncClient) -> None:
    team_a = await _create_team("t-a")
    team_b = await _create_team("t-b")
    other_team_channel = await _create_channel(team_b)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_a}/routes",
        json=_notify_body(channel_ids=[other_team_channel]),
    )
    assert resp.status_code == 422


async def test_unknown_cluster_id_is_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-badcluster")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(channel_ids=[await _create_channel(team_id)], clusters=[999999]),
    )
    assert resp.status_code == 422


async def test_get_route_member_ok_outsider_forbidden(app) -> None:
    team_id = await _create_team("t-getroute")
    channel_id = await _create_channel(team_id)

    async with await _fresh_client(app) as owner:
        await login_as(owner, username="bob")
        bob_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")
        create_resp = await owner.post(
            f"/api/v1/teams/{team_id}/routes", json=_notify_body(channel_ids=[channel_id])
        )
        route_id = create_resp.json()["id"]

    async with await _fresh_client(app) as member:
        await login_as(member, username="carol")
        carol_id = (await member.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")
        resp = await member.get(f"/api/v1/routes/{route_id}")
        assert resp.status_code == 200

    async with await _fresh_client(app) as outsider:
        await login_as(outsider, username="dave")
        resp = await outsider.get(f"/api/v1/routes/{route_id}")
        assert resp.status_code == 403


async def test_update_route_replaces_matchers_and_channels(client: AsyncClient) -> None:
    team_id = await _create_team("t-update")
    channel_1 = await _create_channel(team_id, "c1")
    channel_2 = await _create_channel(team_id, "c2")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    create_resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[channel_1],
            matchers=[{"kind": "include", "target": "alertname", "pattern": "Cpu"}],
        ),
    )
    route_id = create_resp.json()["id"]

    update_resp = await client.put(
        f"/api/v1/routes/{route_id}",
        json=_notify_body(
            name="notify-rule",
            channel_ids=[channel_2],
            matchers=[
                {"kind": "exclude", "target": "alertname", "pattern": "Noisy"},
            ],
        ),
    )
    assert update_resp.status_code == 200
    body = update_resp.json()
    assert body["channel_ids"] == [channel_2]
    assert len(body["matchers"]) == 1
    assert body["matchers"][0]["kind"] == "exclude"
    assert body["matchers"][0]["pattern"] == "Noisy"

    async with db_module.async_session_factory() as session:
        rule = await session.get(RoutingRule, route_id)
        assert rule is not None


async def test_update_route_by_outsider_is_403(app) -> None:
    team_id = await _create_team("t-update-outsider")
    channel_id = await _create_channel(team_id)

    async with await _fresh_client(app) as owner:
        await login_as(owner, username="bob")
        bob_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")
        create_resp = await owner.post(
            f"/api/v1/teams/{team_id}/routes", json=_notify_body(channel_ids=[channel_id])
        )
        route_id = create_resp.json()["id"]

    async with await _fresh_client(app) as outsider:
        await login_as(outsider, username="dave")
        resp = await outsider.put(
            f"/api/v1/routes/{route_id}",
            json=_notify_body(name="hacked", channel_ids=[channel_id]),
        )
        assert resp.status_code == 403

    async with db_module.async_session_factory() as session:
        rule = await session.get(RoutingRule, route_id)
        assert rule.name != "hacked"


async def test_delete_route_owner_only(app) -> None:
    team_id = await _create_team("t-delroute")
    channel_id = await _create_channel(team_id)

    async with await _fresh_client(app) as owner:
        await login_as(owner, username="bob")
        bob_id = (await owner.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, bob_id, "owner")
        create_resp = await owner.post(
            f"/api/v1/teams/{team_id}/routes", json=_notify_body(channel_ids=[channel_id])
        )
        route_id = create_resp.json()["id"]

    async with await _fresh_client(app) as member:
        await login_as(member, username="carol")
        carol_id = (await member.get("/api/v1/auth/me")).json()["id"]
        await _add_membership(team_id, carol_id, "member")
        resp = await member.delete(f"/api/v1/routes/{route_id}")
        assert resp.status_code == 403

    async with await _fresh_client(app) as owner2:
        await login_as(owner2, username="bob")
        resp = await owner2.delete(f"/api/v1/routes/{route_id}")
        assert resp.status_code == 204

    async with db_module.async_session_factory() as session:
        assert await session.get(RoutingRule, route_id) is None


async def test_preview_evaluates_recent_events_with_correct_verdicts(client: AsyncClient) -> None:
    team_id = await _create_team("t-preview")
    cluster_id = await _create_cluster()
    critical_event = await _create_event(
        team_id, cluster_id, alertname="HighCpu", severity="critical", fingerprint="fp-crit"
    )
    info_event = await _create_event(
        team_id, cluster_id, alertname="LowDisk", severity="info", fingerprint="fp-info"
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes/preview",
        json=_notify_body(channel_ids=[], severities=["critical"]),
    )
    assert resp.status_code == 200
    by_id = {row["event_id"]: row for row in resp.json()}
    assert by_id[critical_event]["verdict"] == "matched"
    assert by_id[info_event]["verdict"] == "severity_filtered"


async def test_preview_reports_blocking_matcher_position(client: AsyncClient) -> None:
    team_id = await _create_team("t-preview2")
    cluster_id = await _create_cluster()
    event_id = await _create_event(
        team_id, cluster_id, alertname="HighCpu", fingerprint="fp-1"
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes/preview",
        json=_notify_body(
            channel_ids=[],
            matchers=[{"kind": "include", "target": "alertname", "pattern": "Mem"}],
        ),
    )
    assert resp.status_code == 200
    [row] = [r for r in resp.json() if r["event_id"] == event_id]
    assert row["verdict"] == "not_included"
    assert row["blocking_matcher_position"] == 0


async def test_preview_includes_stored_status_but_evaluates_as_firing(client: AsyncClient) -> None:
    """Preview always evaluates as if the alert had just fired, regardless
    of an event's actual current status -- but the response still surfaces
    that real status so results stay interpretable.
    """
    team_id = await _create_team("t-preview-status")
    cluster_id = await _create_cluster()
    resolved_event = await _create_event(
        team_id, cluster_id, alertname="HighCpu", fingerprint="fp-resolved", status="resolved"
    )
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    # notify_on_firing (default True) is what a firing-trigger evaluation
    # gates on; notify_on_resolved defaults False. If preview evaluated
    # using the event's actual (resolved) status instead of firing, this
    # would come back gated instead of matched.
    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes/preview",
        json=_notify_body(channel_ids=[]),
    )
    assert resp.status_code == 200
    [row] = [r for r in resp.json() if r["event_id"] == resolved_event]
    assert row["status"] == "resolved"
    assert row["verdict"] == "matched"


# -- Phase 15: escalation / renotify ------------------------------------


async def test_escalation_enabled_requires_after_minutes(client: AsyncClient) -> None:
    team_id = await _create_team("t-esc-nomin")
    channel_id = await _create_channel(team_id)
    esc_channel_id = await _create_channel(team_id, "esc")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[channel_id],
            escalation_enabled=True,
            escalation_channel_ids=[esc_channel_id],
        ),
    )
    assert resp.status_code == 422


async def test_escalation_enabled_requires_at_least_one_channel(client: AsyncClient) -> None:
    team_id = await _create_team("t-esc-nochan")
    channel_id = await _create_channel(team_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[channel_id], escalation_enabled=True, escalation_after_minutes=5
        ),
    )
    assert resp.status_code == 422


async def test_escalation_and_renotify_round_trip(client: AsyncClient) -> None:
    team_id = await _create_team("t-esc-ok")
    channel_id = await _create_channel(team_id)
    esc_channel_id = await _create_channel(team_id, "esc")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            channel_ids=[channel_id],
            escalation_enabled=True,
            escalation_after_minutes=15,
            escalation_channel_ids=[esc_channel_id],
            renotify_interval_minutes=30,
        ),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["escalation_enabled"] is True
    assert body["escalation_after_minutes"] == 15
    assert body["escalation_channel_ids"] == [esc_channel_id]
    assert body["renotify_interval_minutes"] == 30

    get_resp = await client.get(f"/api/v1/routes/{body['id']}")
    assert get_resp.json()["escalation_channel_ids"] == [esc_channel_id]


async def test_escalation_channel_from_other_team_without_opt_in_is_422(
    client: AsyncClient,
) -> None:
    team_a = await _create_team("t-esc-a")
    team_b = await _create_team("t-esc-b")
    channel_id = await _create_channel(team_a)
    other_team_channel = await _create_channel(team_b, allow_cross_team_escalation=False)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_a}/routes",
        json=_notify_body(
            channel_ids=[channel_id],
            escalation_enabled=True,
            escalation_after_minutes=5,
            escalation_channel_ids=[other_team_channel],
        ),
    )
    assert resp.status_code == 422


async def test_escalation_channel_from_other_team_with_opt_in_succeeds(
    client: AsyncClient,
) -> None:
    team_a = await _create_team("t-esc-c")
    team_b = await _create_team("t-esc-d")
    channel_id = await _create_channel(team_a)
    other_team_channel = await _create_channel(team_b, allow_cross_team_escalation=True)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_a}/routes",
        json=_notify_body(
            channel_ids=[channel_id],
            escalation_enabled=True,
            escalation_after_minutes=5,
            escalation_channel_ids=[other_team_channel],
        ),
    )
    assert resp.status_code == 201
    assert resp.json()["escalation_channel_ids"] == [other_team_channel]


async def test_suppress_rule_with_escalation_is_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-esc-suppress")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(
            action="suppress", channel_ids=[], escalation_enabled=True, escalation_after_minutes=5
        ),
    )
    assert resp.status_code == 422


async def test_suppress_rule_with_renotify_is_422(client: AsyncClient) -> None:
    team_id = await _create_team("t-renotify-suppress")
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(action="suppress", channel_ids=[], renotify_interval_minutes=10),
    )
    assert resp.status_code == 422


async def test_renotify_interval_must_be_positive(client: AsyncClient) -> None:
    team_id = await _create_team("t-renotify-badval")
    channel_id = await _create_channel(team_id)
    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    resp = await client.post(
        f"/api/v1/teams/{team_id}/routes",
        json=_notify_body(channel_ids=[channel_id], renotify_interval_minutes=0),
    )
    assert resp.status_code == 422
