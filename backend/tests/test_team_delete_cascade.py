"""Deleting a team must not orphan its PrometheusRules on any enabled
cluster: they're cleaned up first, and the whole operation is all-or-
nothing if a cluster can't be reached.
"""

from typing import Any
from unittest.mock import MagicMock

from httpx import AsyncClient
from kubernetes.client.exceptions import ApiException
from sqlalchemy import select

import app.db as db_module
from app.models.audit import AuditLog
from app.models.team import Team, TeamMembership
from tests.conftest import login_as

ADMIN_DN = "cn=kam-admins,ou=groups,dc=example,dc=org"


def _owned_rule(name: str, team_id: str) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": "kam-rules",
            "resourceVersion": "1",
            "labels": {"app.kubernetes.io/managed-by": "kam", "kam/team-id": team_id},
        },
        "spec": {"groups": [{"name": "g", "rules": [{"alert": "X", "expr": "up"}]}]},
    }


async def _create_team(slug: str = "doomed") -> int:
    async with db_module.async_session_factory() as session:
        team = Team(slug=slug, name=slug.title())
        session.add(team)
        await session.commit()
        await session.refresh(team)
        return team.id


async def _add_membership(client: AsyncClient, team_id: int, role: str = "owner") -> None:
    me = (await client.get("/api/v1/auth/me")).json()
    async with db_module.async_session_factory() as session:
        session.add(
            TeamMembership(team_id=team_id, user_id=me["id"], role=role, origin="manual")
        )
        await session.commit()


def _patch_co_api(app, fake_api: MagicMock) -> None:
    app.state.k8s_factory._co_api = lambda cluster: fake_api


async def test_delete_team_deletes_its_rules_on_every_enabled_cluster_first(
    client: AsyncClient, app
) -> None:
    team_id = await _create_team()

    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {
        "items": [
            _owned_rule(f"kam-t{team_id}-one", str(team_id)),
            _owned_rule(f"kam-t{team_id}-two", str(team_id)),
        ]
    }
    fake_api.get_namespaced_custom_object.side_effect = (
        lambda **kwargs: _owned_rule(kwargs["name"], str(team_id))
    )
    _patch_co_api(app, fake_api)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/teams/{team_id}")

    assert response.status_code == 204
    assert fake_api.delete_namespaced_custom_object.call_count == 2
    deleted_names = {
        call.kwargs["name"] for call in fake_api.delete_namespaced_custom_object.call_args_list
    }
    assert deleted_names == {f"kam-t{team_id}-one", f"kam-t{team_id}-two"}

    async with db_module.async_session_factory() as session:
        team = await session.get(Team, team_id)
        assert team is None

        # Note: team_id isn't queryable here -- deleting the team in the same
        # transaction triggers the audit_logs.team_id ON DELETE SET NULL
        # cascade for every row that referenced it, including the ones just
        # inserted above (see AuditLog's docstring). Query by object_ref
        # instead.
        rule_delete_rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "rule.delete",
                    AuditLog.object_ref.in_([f"kam-t{team_id}-one", f"kam-t{team_id}-two"]),
                )
            )
        ).scalars().all()
        assert len(rule_delete_rows) == 2
        assert {row.detail["reason"] for row in rule_delete_rows} == {"team_delete"}

        team_delete_rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "team.delete", AuditLog.object_ref == "doomed"
                )
            )
        ).scalars().all()
        assert len(team_delete_rows) == 1


async def test_delete_team_aborts_with_503_when_a_cluster_is_unreachable(
    client: AsyncClient, app
) -> None:
    team_id = await _create_team()

    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.side_effect = ApiException(status=500)
    _patch_co_api(app, fake_api)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/teams/{team_id}")

    assert response.status_code == 503
    fake_api.delete_namespaced_custom_object.assert_not_called()

    async with db_module.async_session_factory() as session:
        team = await session.get(Team, team_id)
        assert team is not None

        team_delete_rows = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "team.delete")
            )
        ).scalars().all()
        assert len(team_delete_rows) == 0


async def test_delete_team_aborts_with_503_when_cluster_drops_out_mid_loop(
    client: AsyncClient, app
) -> None:
    """The pre-flight list succeeds (cluster looked reachable), but the
    cluster stops responding partway through actually deleting the rules.
    That must abort the whole request with 503 -- not just skip the failed
    rule and delete the team anyway."""
    team_id = await _create_team()

    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {
        "items": [
            _owned_rule(f"kam-t{team_id}-one", str(team_id)),
            _owned_rule(f"kam-t{team_id}-two", str(team_id)),
        ]
    }
    fake_api.get_namespaced_custom_object.side_effect = (
        lambda **kwargs: _owned_rule(kwargs["name"], str(team_id))
    )
    fake_api.delete_namespaced_custom_object.side_effect = [
        None,
        ApiException(status=500),
    ]
    _patch_co_api(app, fake_api)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/teams/{team_id}")

    assert response.status_code == 503
    assert fake_api.delete_namespaced_custom_object.call_count == 2

    async with db_module.async_session_factory() as session:
        assert await session.get(Team, team_id) is not None

        team_delete_rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "team.delete"))
        ).scalars().all()
        assert len(team_delete_rows) == 0


async def test_delete_team_continues_past_rule_specific_failures(
    client: AsyncClient, app
) -> None:
    """A rule-specific failure (here: a bad request on one particular
    delete call) says nothing about the cluster's health -- it shouldn't
    block cleanup of the rest or the team deletion itself."""
    team_id = await _create_team()

    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {
        "items": [
            _owned_rule(f"kam-t{team_id}-one", str(team_id)),
            _owned_rule(f"kam-t{team_id}-two", str(team_id)),
        ]
    }
    fake_api.get_namespaced_custom_object.side_effect = (
        lambda **kwargs: _owned_rule(kwargs["name"], str(team_id))
    )
    fake_api.delete_namespaced_custom_object.side_effect = [
        ApiException(status=400),
        None,
    ]
    _patch_co_api(app, fake_api)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/teams/{team_id}")

    assert response.status_code == 204
    assert fake_api.delete_namespaced_custom_object.call_count == 2

    async with db_module.async_session_factory() as session:
        assert await session.get(Team, team_id) is None


async def test_delete_team_with_no_rules_still_deletes_the_team(
    client: AsyncClient, app
) -> None:
    team_id = await _create_team(slug="empty-team")

    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {"items": []}
    _patch_co_api(app, fake_api)

    await login_as(client, username="alice", group_dns=[ADMIN_DN])

    response = await client.delete(f"/api/v1/teams/{team_id}")

    assert response.status_code == 204
    fake_api.delete_namespaced_custom_object.assert_not_called()

    async with db_module.async_session_factory() as session:
        assert await session.get(Team, team_id) is None


async def test_non_admin_cannot_delete_team(client: AsyncClient) -> None:
    team_id = await _create_team()
    await login_as(client, username="bob")
    await _add_membership(client, team_id)

    response = await client.delete(f"/api/v1/teams/{team_id}")
    assert response.status_code == 403
