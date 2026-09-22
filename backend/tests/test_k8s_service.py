"""Direct unit tests of K8sClientFactory's PrometheusRule operations,
bypassing the API layer entirely -- these exist specifically to prove the
ownership guard is enforced *inside the service*, not just checked by the
API handlers. Mocks the kubernetes CustomObjectsApi at the `_co_api`/
`_core_api` seam.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from app.models.cluster import Cluster
from app.services.k8s import (
    K8sClientFactory,
    K8sUnavailableError,
    RuleConflictError,
    RuleForbiddenError,
)


def _cluster() -> Cluster:
    return Cluster(
        id=1,
        name="local",
        display_name="local",
        k8s_auth_kind="kubeconfig",
        prometheus_url="http://localhost:30090",
        alertmanager_url="http://localhost:30093",
        rules_namespace="kam-rules",
        webhook_token_hash="x",
    )


def _owned_rule(name: str = "kam-platform-x", team_id: str = "1") -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "namespace": "kam-rules",
            "resourceVersion": "42",
            "labels": {"app.kubernetes.io/managed-by": "kam", "kam/team-id": team_id},
        },
        "spec": {"groups": [{"name": "g", "rules": [{"alert": "X", "expr": "up"}]}]},
    }


def _factory_with_fake_co_api(fake_api: MagicMock) -> K8sClientFactory:
    factory = K8sClientFactory()
    factory._co_api = lambda cluster: fake_api  # type: ignore[method-assign]
    return factory


async def test_list_rules_passes_managed_by_and_team_id_label_selector() -> None:
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.return_value = {"items": [_owned_rule()]}
    factory = _factory_with_fake_co_api(fake_api)

    result = await factory.list_rules(_cluster(), team_id=1)

    assert result == [_owned_rule()]
    _, kwargs = fake_api.list_namespaced_custom_object.call_args
    assert kwargs["namespace"] == "kam-rules"
    assert kwargs["label_selector"] == "app.kubernetes.io/managed-by=kam,kam/team-id=1"


async def test_get_rule_returns_none_on_404() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    factory = _factory_with_fake_co_api(fake_api)

    assert await factory.get_rule(_cluster(), "missing") is None


async def test_get_rule_raises_unavailable_on_other_api_errors() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=500)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sUnavailableError):
        await factory.get_rule(_cluster(), "x")


async def test_create_rule_raises_conflict_on_409() -> None:
    fake_api = MagicMock()
    fake_api.create_namespaced_custom_object.side_effect = ApiException(status=409)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(RuleConflictError):
        await factory.create_rule(_cluster(), {"metadata": {"name": "kam-platform-x"}})


async def test_replace_rule_succeeds_when_labels_match_team() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    fake_api.replace_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    factory = _factory_with_fake_co_api(fake_api)

    result = await factory.replace_rule(
        _cluster(), "kam-platform-x", team_id=1, body={"metadata": {}}
    )

    assert result["metadata"]["labels"]["kam/team-id"] == "1"
    # resourceVersion from the fetched object must be carried into the body
    # sent to the API server (required for the CRD replace to succeed).
    _, kwargs = fake_api.replace_namespaced_custom_object.call_args
    assert kwargs["body"]["metadata"]["resourceVersion"] == "42"


async def test_replace_rule_rejects_foreign_team_id() -> None:
    """The core security guard: a rule owned by a different team must never
    be mutated, even if the caller somehow got past API-layer RBAC."""
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="99")
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(RuleForbiddenError):
        await factory.replace_rule(
            _cluster(), "kam-platform-x", team_id=1, body={"metadata": {}}
        )
    fake_api.replace_namespaced_custom_object.assert_not_called()


async def test_replace_rule_rejects_missing_managed_by_label() -> None:
    """A hand-created PrometheusRule with no kam labels at all -- e.g. a
    cluster admin's own rule sharing the rules namespace -- must also never
    be touched, regardless of team_id."""
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = {
        "metadata": {"name": "some-other-rule", "labels": {}},
        "spec": {"groups": []},
    }
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(RuleForbiddenError):
        await factory.replace_rule(
            _cluster(), "some-other-rule", team_id=1, body={"metadata": {}}
        )
    fake_api.replace_namespaced_custom_object.assert_not_called()


async def test_replace_rule_rejects_when_rule_does_not_exist() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=404)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(RuleForbiddenError):
        await factory.replace_rule(_cluster(), "ghost", team_id=1, body={"metadata": {}})
    fake_api.replace_namespaced_custom_object.assert_not_called()


async def test_delete_rule_rejects_foreign_team_id() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="2")
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(RuleForbiddenError):
        await factory.delete_rule(_cluster(), "kam-platform-x", team_id=1)
    fake_api.delete_namespaced_custom_object.assert_not_called()


async def test_delete_rule_succeeds_when_owned() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    factory = _factory_with_fake_co_api(fake_api)

    await factory.delete_rule(_cluster(), "kam-platform-x", team_id=1)
    fake_api.delete_namespaced_custom_object.assert_called_once()


async def test_list_namespaces_uses_core_api() -> None:
    fake_core_api = MagicMock()
    ns_a = MagicMock()
    ns_a.metadata.name = "default"
    ns_b = MagicMock()
    ns_b.metadata.name = "kam-rules"
    fake_core_api.list_namespace.return_value = MagicMock(items=[ns_a, ns_b])

    factory = K8sClientFactory()
    factory._core_api = lambda cluster: fake_core_api  # type: ignore[method-assign]

    result = await factory.list_namespaces(_cluster())
    assert result == ["default", "kam-rules"]
