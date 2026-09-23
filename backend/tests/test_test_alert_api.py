"""API tests for POST /teams/{id}/test-alert and POST
/alerts/history/{id}/resolve-test: the synthetic-alert firing pipeline,
verdict reporting, outbox staging, and the resolved-transition endpoint.
"""

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import select

import app.db as db_module
from app.models.alert import AlertEvent
from app.models.channel import Channel
from app.models.cluster import Cluster
from app.models.outbox import NotificationOutbox
from app.models.routing import RoutingRule
from app.models.team import Team, TeamMembership
from app.security import encrypt_str
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


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


async def _create_channel(team_id: int, name: str = "email-1") -> int:
    async with db_module.async_session_factory() as session:
        channel = Channel(
            team_id=team_id,
            name=name,
            type="email",
            config_encrypted=encrypt_str('{"recipients": ["ops@example.org"]}'),
        )
        session.add(channel)
        await session.commit()
        await session.refresh(channel)
        return channel.id


async def _create_notify_rule(
    team_id: int,
    channel_id: int,
    *,
    name: str = "notify-warning",
    severities: list[str] | None = None,
    enabled: bool = True,
    notify_on_resolved: bool = False,
) -> int:
    async with db_module.async_session_factory() as session:
        channel = await session.get(Channel, channel_id)
        rule = RoutingRule(
            team_id=team_id,
            name=name,
            action="notify",
            enabled=enabled,
            notify_on_firing=True,
            notify_on_resolved=notify_on_resolved,
            severities=severities,
            channels=[channel] if channel else [],
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
        return rule.id


async def _create_suppress_rule(
    team_id: int, *, name: str = "suppress-all", severities: list[str] | None = None
) -> int:
    async with db_module.async_session_factory() as session:
        rule = RoutingRule(
            team_id=team_id,
            name=name,
            action="suppress",
            enabled=True,
            severities=severities,
        )
        session.add(rule)
        await session.commit()
        await session.refresh(rule)
        return rule.id


async def _user_id(client: AsyncClient) -> int:
    return (await client.get("/api/v1/auth/me")).json()["id"]


async def _create_plain_event(*, cluster_id: int, team_id: int, fingerprint: str = "f1") -> int:
    async with db_module.async_session_factory() as session:
        cluster = await session.get(Cluster, cluster_id)
        event = AlertEvent(
            cluster_id=cluster_id,
            cluster_name=cluster.name,
            fingerprint=fingerprint,
            status="firing",
            alertname="RealAlert",
            severity="critical",
            labels={},
            annotations={},
            team_id=team_id,
            starts_at=datetime.now(UTC),
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        return event.id


# -- firing -----------------------------------------------------------------


async def test_test_alert_requires_team_member(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()

    await login_as(client, username="carol")
    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id}
    )
    assert response.status_code == 403


async def test_test_alert_creates_is_test_event_with_team_and_labels(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert",
        json={"cluster_id": cluster_id, "namespace": "kam-demo", "labels": {"custom": "x"}},
    )
    assert response.status_code == 200
    event_id = response.json()["event_id"]

    detail = await client.get(f"/api/v1/alerts/history/{event_id}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["is_test"] is True
    assert body["team_id"] == team_id
    assert body["status"] == "firing"
    assert body["namespace"] == "kam-demo"
    assert body["labels"]["kam_test"] == "true"
    assert body["labels"]["kam_team"] == "platform"
    assert body["labels"]["custom"] == "x"
    assert body["labels"]["alertname"] == "KamTestAlert"
    assert body["labels"]["severity"] == "warning"
    assert body["fingerprint"].startswith("test-")


async def test_test_alert_user_cannot_override_kam_team_or_kam_test(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert",
        json={
            "cluster_id": cluster_id,
            "labels": {"kam_team": "someone-else", "kam_test": "false"},
        },
    )
    event_id = response.json()["event_id"]
    detail = (await client.get(f"/api/v1/alerts/history/{event_id}")).json()
    assert detail["labels"]["kam_team"] == "platform"
    assert detail["labels"]["kam_test"] == "true"
    assert detail["team_id"] == team_id


async def test_test_alert_verdicts_and_outbox_delivery(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()
    channel_id = await _create_channel(team_id)

    matching_rule_id = await _create_notify_rule(
        team_id, channel_id, name="notify-warning", severities=["warning"]
    )
    non_matching_rule_id = await _create_notify_rule(
        team_id, channel_id, name="notify-critical-only", severities=["critical"]
    )
    disabled_rule_id = await _create_notify_rule(
        team_id, channel_id, name="disabled-rule", enabled=False
    )

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id}
    )
    assert response.status_code == 200
    body = response.json()

    verdicts_by_rule = {v["rule_id"]: v for v in body["verdicts"]}
    assert verdicts_by_rule[matching_rule_id]["verdict"] == "matched"
    assert verdicts_by_rule[matching_rule_id]["action"] == "notify"
    assert verdicts_by_rule[non_matching_rule_id]["verdict"] == "severity_filtered"
    # A disabled rule is entirely excluded from the verdicts list, matching
    # route_event's own enabled=True filter.
    assert disabled_rule_id not in verdicts_by_rule

    assert body["delivered_channels"] == ["email-1"]

    async with db_module.async_session_factory() as session:
        outbox_rows = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.alert_event_id == body["event_id"]
                )
            )
        ).scalars().all()
        assert len(outbox_rows) == 1
        assert outbox_rows[0].trigger == "firing"
        assert outbox_rows[0].status == "pending"


async def test_test_alert_reports_suppressed_by_and_empty_delivered_channels(
    client: AsyncClient,
) -> None:
    """route_event short-circuits entirely on the first matching suppress
    rule -- a notify rule can still independently evaluate as "matched" in
    the verdicts list (it has no visibility into that short-circuit), but
    nothing actually gets staged. The response must say which rule did the
    suppressing so the UI doesn't present a matched-but-undelivered notify
    verdict as if it had gone out.
    """
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()
    channel_id = await _create_channel(team_id)

    notify_rule_id = await _create_notify_rule(
        team_id, channel_id, name="notify-warning", severities=["warning"]
    )
    suppress_rule_id = await _create_suppress_rule(
        team_id, name="suppress-warning", severities=["warning"]
    )

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["suppressed_by"] == {"rule_id": suppress_rule_id, "rule_name": "suppress-warning"}
    assert body["delivered_channels"] == []

    verdicts_by_rule = {v["rule_id"]: v for v in body["verdicts"]}
    # The notify rule's own evaluate() still independently reports
    # "matched" -- it's route_event's staging that was actually
    # short-circuited, which is exactly what suppressed_by communicates.
    assert verdicts_by_rule[notify_rule_id]["verdict"] == "matched"
    assert verdicts_by_rule[suppress_rule_id]["verdict"] == "matched"

    async with db_module.async_session_factory() as session:
        outbox_rows = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.alert_event_id == body["event_id"]
                )
            )
        ).scalars().all()
        assert outbox_rows == []


async def test_test_alert_no_suppressed_by_when_nothing_suppresses(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id}
    )
    assert response.json()["suppressed_by"] is None


async def test_test_alert_defaults_alertname_and_severity(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id}
    )
    event_id = response.json()["event_id"]
    detail = (await client.get(f"/api/v1/alerts/history/{event_id}")).json()
    assert detail["alertname"] == "KamTestAlert"
    assert detail["severity"] == "warning"


async def test_test_alert_unknown_cluster_404(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": 999999}
    )
    assert response.status_code == 404


# -- history include_test ---------------------------------------------------


async def test_history_excludes_test_alerts_by_default(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()
    await login_as(client, username="alice", group_dns=[ADMIN_DN])
    await _add_membership(team_id, await _user_id(client))

    await client.post(f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id})
    await _create_plain_event(cluster_id=cluster_id, team_id=team_id, fingerprint="real-1")

    default_response = await client.get(f"/api/v1/alerts/history?team_id={team_id}")
    assert [i["alertname"] for i in default_response.json()["items"]] == ["RealAlert"]

    included_response = await client.get(
        f"/api/v1/alerts/history?team_id={team_id}&include_test=true"
    )
    names = {i["alertname"] for i in included_response.json()["items"]}
    assert names == {"RealAlert", "KamTestAlert"}


# -- resolve-test -------------------------------------------------------


async def test_resolve_test_rejects_non_test_event_422(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()
    event_id = await _create_plain_event(cluster_id=cluster_id, team_id=team_id)

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    response = await client.post(f"/api/v1/alerts/history/{event_id}/resolve-test")
    assert response.status_code == 422


async def test_resolve_test_transitions_and_routes_resolved(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()
    channel_id = await _create_channel(team_id)
    await _create_notify_rule(
        team_id, channel_id, severities=["warning"], notify_on_resolved=True
    )

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    fire = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id}
    )
    event_id = fire.json()["event_id"]

    resolve = await client.post(f"/api/v1/alerts/history/{event_id}/resolve-test")
    assert resolve.status_code == 200
    body = resolve.json()
    assert body["status"] == "resolved"
    assert body["ends_at"] is not None

    async with db_module.async_session_factory() as session:
        outbox_rows = (
            await session.execute(
                select(NotificationOutbox).where(NotificationOutbox.alert_event_id == event_id)
            )
        ).scalars().all()
        triggers = sorted(row.trigger for row in outbox_rows)
        assert triggers == ["firing", "resolved"]


async def test_resolve_test_is_noop_when_already_resolved(client: AsyncClient) -> None:
    team_id = await _create_team("platform")
    cluster_id = await _default_cluster_id()

    await login_as(client, username="alice")
    await _add_membership(team_id, await _user_id(client))

    fire = await client.post(
        f"/api/v1/teams/{team_id}/test-alert", json={"cluster_id": cluster_id}
    )
    event_id = fire.json()["event_id"]

    first = await client.post(f"/api/v1/alerts/history/{event_id}/resolve-test")
    ends_at_first = first.json()["ends_at"]

    second = await client.post(f"/api/v1/alerts/history/{event_id}/resolve-test")
    assert second.status_code == 200
    assert second.json()["ends_at"] == ends_at_first  # unchanged, not re-stamped
