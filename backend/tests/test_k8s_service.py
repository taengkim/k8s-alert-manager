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
    K8sBadRequestError,
    K8sClientFactory,
    K8sUnavailableError,
    RuleConflictError,
    RuleForbiddenError,
    RuleUpdateConflictError,
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


async def test_replace_rule_does_not_mutate_callers_body_dict() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    fake_api.replace_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    factory = _factory_with_fake_co_api(fake_api)

    original_body = {"metadata": {"name": "kam-platform-x"}, "spec": {}}
    await factory.replace_rule(_cluster(), "kam-platform-x", team_id=1, body=original_body)

    # The resourceVersion pin must land on a copy, not the caller's dict.
    assert "resourceVersion" not in original_body["metadata"]


async def test_replace_rule_raises_unavailable_when_existing_rule_has_no_resource_version() -> None:
    fake_api = MagicMock()
    rule_without_rv = _owned_rule(team_id="1")
    del rule_without_rv["metadata"]["resourceVersion"]
    fake_api.get_namespaced_custom_object.return_value = rule_without_rv
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sUnavailableError):
        await factory.replace_rule(
            _cluster(), "kam-platform-x", team_id=1, body={"metadata": {}}
        )
    fake_api.replace_namespaced_custom_object.assert_not_called()


async def test_replace_rule_maps_409_on_write_to_update_conflict() -> None:
    """A 409 from the actual replace call (as opposed to the initial
    ownership-check fetch) means someone else wrote this rule between our
    read and write -- a different situation from RuleConflictError (which is
    about *creating* a name that already exists)."""
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    fake_api.replace_namespaced_custom_object.side_effect = ApiException(status=409)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(RuleUpdateConflictError):
        await factory.replace_rule(
            _cluster(), "kam-platform-x", team_id=1, body={"metadata": {}}
        )


async def test_replace_rule_maps_other_4xx_to_bad_request() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    fake_api.replace_namespaced_custom_object.side_effect = ApiException(status=400)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sBadRequestError):
        await factory.replace_rule(
            _cluster(), "kam-platform-x", team_id=1, body={"metadata": {}}
        )


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


async def test_delete_rule_sends_resource_version_precondition() -> None:
    """TOCTOU guard: the delete call must be conditioned on the
    resourceVersion we just read, so a rule that changed between our
    ownership-check read and the delete itself fails instead of silently
    deleting whatever it became."""
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    factory = _factory_with_fake_co_api(fake_api)

    await factory.delete_rule(_cluster(), "kam-platform-x", team_id=1)

    _, kwargs = fake_api.delete_namespaced_custom_object.call_args
    delete_options = kwargs["body"]
    assert delete_options.preconditions.resource_version == "42"


async def test_delete_rule_raises_unavailable_when_existing_rule_has_no_resource_version() -> None:
    fake_api = MagicMock()
    rule_without_rv = _owned_rule(team_id="1")
    del rule_without_rv["metadata"]["resourceVersion"]
    fake_api.get_namespaced_custom_object.return_value = rule_without_rv
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sUnavailableError):
        await factory.delete_rule(_cluster(), "kam-platform-x", team_id=1)
    fake_api.delete_namespaced_custom_object.assert_not_called()


async def test_delete_rule_maps_409_to_update_conflict() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    fake_api.delete_namespaced_custom_object.side_effect = ApiException(status=409)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(RuleUpdateConflictError):
        await factory.delete_rule(_cluster(), "kam-platform-x", team_id=1)


async def test_delete_rule_treats_404_on_the_delete_call_as_already_done() -> None:
    """A 404 on the delete call itself (as opposed to the initial
    ownership-check fetch, which already confirmed the rule existed) means
    something else deleted it a moment ago -- the desired end state (rule
    absent) is already achieved, so this should not raise."""
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    fake_api.delete_namespaced_custom_object.side_effect = ApiException(status=404)
    factory = _factory_with_fake_co_api(fake_api)

    await factory.delete_rule(_cluster(), "kam-platform-x", team_id=1)


async def test_delete_rule_maps_other_4xx_to_bad_request() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.return_value = _owned_rule(team_id="1")
    fake_api.delete_namespaced_custom_object.side_effect = ApiException(status=400)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sBadRequestError):
        await factory.delete_rule(_cluster(), "kam-platform-x", team_id=1)


async def test_list_rules_maps_other_4xx_to_bad_request() -> None:
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.side_effect = ApiException(status=400)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sBadRequestError):
        await factory.list_rules(_cluster(), team_id=1)


async def test_get_rule_maps_other_4xx_to_bad_request() -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=400)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sBadRequestError):
        await factory.get_rule(_cluster(), "x")


@pytest.mark.parametrize("status_code", [400, 405, 415, 422])
async def test_get_rule_maps_bad_request_statuses(status_code: int) -> None:
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=status_code)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sBadRequestError):
        await factory.get_rule(_cluster(), "x")


@pytest.mark.parametrize("status_code", [401, 403, 429, 418, 500, 502])
async def test_get_rule_maps_everything_else_to_unavailable(status_code: int) -> None:
    """401/403/429 (and anything else outside the narrow bad-request set)
    reflect something wrong reaching/using the cluster, not a malformed
    request of ours -- they must not be treated as our fault (422)."""
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = ApiException(status=status_code)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sUnavailableError):
        await factory.get_rule(_cluster(), "x")


async def test_list_rules_treats_404_as_unavailable_not_bad_request() -> None:
    """A 404 on the list call itself almost certainly means the
    PrometheusRule CRD isn't installed on this cluster -- a cluster-level
    problem, not something wrong with our request."""
    fake_api = MagicMock()
    fake_api.list_namespaced_custom_object.side_effect = ApiException(status=404)
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sUnavailableError, match="CRD"):
        await factory.list_rules(_cluster(), team_id=1)


async def test_mapped_error_never_embeds_raw_response_headers_or_body() -> None:
    """The client-facing detail must come from exc.reason + the JSON body's
    `message` field only -- never str(exc), which embeds the full HTTP
    response (every header plus the raw body verbatim)."""
    exc = ApiException(status=403, reason="Forbidden")
    exc.body = '{"kind":"Status","message":"safe curated message"}'
    exc.headers = {"Audit-Id": "super-secret-audit-trace-id"}
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = exc
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sUnavailableError) as exc_info:
        await factory.get_rule(_cluster(), "x")

    detail = str(exc_info.value)
    assert "Forbidden" in detail
    assert "safe curated message" in detail
    assert "Audit-Id" not in detail
    assert "super-secret-audit-trace-id" not in detail
    assert "HTTP response headers" not in detail


async def test_mapped_error_detail_falls_back_to_reason_when_body_unparseable() -> None:
    exc = ApiException(status=400, reason="Bad Request")
    exc.body = "not json at all"
    fake_api = MagicMock()
    fake_api.get_namespaced_custom_object.side_effect = exc
    factory = _factory_with_fake_co_api(fake_api)

    with pytest.raises(K8sBadRequestError) as exc_info:
        await factory.get_rule(_cluster(), "x")

    assert str(exc_info.value) == "Bad Request"


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
