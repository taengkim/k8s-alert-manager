"""Unit tests for app.services.rule_transfer: the export envelope's round
trip through parse_envelope, and the plan_import/execute_import pipeline
(k8s mocked at the CustomObjectsApi seam, Prometheus mocked with respx --
same seams test_rules_api.py and test_rules_service.py already use).
"""

from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from kubernetes.client.exceptions import ApiException

from app.models.cluster import Cluster
from app.models.team import Team
from app.services.k8s import K8sClientFactory
from app.services.prometheus import PrometheusUnavailableError
from app.services.rule_transfer import (
    ParsedRuleEntry,
    UnsupportedExportVersion,
    build_export_envelope,
    execute_import,
    parse_envelope,
    plan_import,
)
from app.services.rules import (
    BuilderState,
    RuleWrite,
    build_prometheus_rule,
    parse_prometheus_rule,
)

PROM_URL = "http://prom.example.com"
TARGET_PROM_URL = "http://prom-target.example.com"

VALID = httpx.Response(200, json={"status": "success", "data": "vector(1)"})
INVALID = httpx.Response(
    400, json={"status": "error", "errorType": "bad_data", "error": "parse error"}
)


def _team(team_id: int = 1, slug: str = "platform") -> Team:
    return Team(id=team_id, slug=slug, name=slug.title())


def _cluster(cluster_id: int = 1, name: str = "local", prometheus_url: str = PROM_URL) -> Cluster:
    return Cluster(
        id=cluster_id,
        name=name,
        display_name=name,
        prometheus_url=prometheus_url,
        alertmanager_url="http://am.example.com",
        rules_namespace="kam-rules",
    )


class FakeCoApi:
    """A minimal fake CustomObjectsApi: `existing` is the set of object
    *names* already present on the (simulated) cluster, each "owned" by
    `owner_team_id`. Records every create/replace call's body for
    assertions."""

    def __init__(self, existing: set[str] | None = None, owner_team_id: int = 1) -> None:
        self.existing = set(existing or set())
        self.owner_team_id = owner_team_id
        self.create_calls: list[dict[str, Any]] = []
        self.replace_calls: list[dict[str, Any]] = []

    def get_namespaced_custom_object(self, *, name: str, **_: Any) -> dict[str, Any]:
        if name not in self.existing:
            raise ApiException(status=404)
        return {
            "metadata": {
                "name": name,
                "resourceVersion": "1",
                "labels": {
                    "app.kubernetes.io/managed-by": "kam",
                    "kam/team-id": str(self.owner_team_id),
                },
            },
            "spec": {
                "groups": [{"name": "g", "rules": [{"alert": "X", "expr": "up", "labels": {}}]}]
            },
        }

    def create_namespaced_custom_object(self, *, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.create_calls.append(body)
        return body

    def replace_namespaced_custom_object(self, *, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.replace_calls.append(body)
        return body


def _factory(fake_api: FakeCoApi) -> K8sClientFactory:
    factory = K8sClientFactory()
    factory._co_api = MagicMock(return_value=fake_api)
    return factory


def _entries(*rules: RuleWrite) -> list[ParsedRuleEntry]:
    return [ParsedRuleEntry(slug=r.slug, rule=r) for r in rules]


def _rule(slug: str = "high-cpu", expr: str = "up") -> RuleWrite:
    return RuleWrite(slug=slug, alert_name="HighCpu", expr=expr, severity="warning")


# -- envelope round trip ----------------------------------------------------


def test_export_round_trips_through_parse_envelope() -> None:
    team = _team()
    cluster = _cluster()
    rule_input = RuleWrite(
        slug="high-cpu",
        alert_name="HighCpu",
        expr="cpu_usage > 0.9",
        for_="10m",
        severity="warning",
        labels={"team_owner": "sre"},
        annotations={"summary": "cpu is high"},
        runbook_url="https://runbooks.example.com/high-cpu",
        grafana_url="https://grafana.example.com/d/cpu",
    )
    parsed = parse_prometheus_rule(build_prometheus_rule(team, rule_input))

    envelope = build_export_envelope(team, cluster, [parsed])
    assert envelope["kam_export_version"] == 1
    assert envelope["kind"] == "rules"
    assert envelope["source"] == {
        "team_slug": "platform",
        "cluster_name": "local",
        "app_version": "dev",
    }

    result = parse_envelope(envelope)
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert entry.error is None
    assert entry.rule is not None
    assert entry.rule.slug == "high-cpu"
    assert entry.rule.expr == "cpu_usage > 0.9"
    assert entry.rule.for_ == "10m"
    assert entry.rule.severity == "warning"
    assert entry.rule.labels == {"team_owner": "sre"}
    assert entry.rule.annotations == {"summary": "cpu is high"}
    assert entry.rule.runbook_url == "https://runbooks.example.com/high-cpu"
    assert entry.rule.grafana_url == "https://grafana.example.com/d/cpu"


def test_export_has_no_ownership_labels_or_name_prefix() -> None:
    team = _team()
    cluster = _cluster()
    parsed = parse_prometheus_rule(build_prometheus_rule(team, _rule(slug="x")))

    envelope = build_export_envelope(team, cluster, [parsed])
    exported = envelope["rules"][0]

    assert exported["slug"] == "x"  # not "kam-t1-x"
    assert exported["labels"] == {}  # kam_team/severity live elsewhere, never here


def test_export_preserves_builder_state() -> None:
    team = _team()
    cluster = _cluster()
    state = BuilderState(metric="node_load1", comparison=">", threshold=0)
    rule_input = RuleWrite(
        slug="high-load",
        alert_name="HighLoad",
        expr="node_load1 > 0",
        severity="warning",
        mode="builder",
        builder_state=state,
    )
    parsed = parse_prometheus_rule(build_prometheus_rule(team, rule_input))

    envelope = build_export_envelope(team, cluster, [parsed])
    result = parse_envelope(envelope)

    assert result.entries[0].rule.mode == "builder"
    assert result.entries[0].rule.builder_state == state


def test_parse_envelope_rejects_unsupported_version() -> None:
    with pytest.raises(UnsupportedExportVersion):
        parse_envelope({"kam_export_version": 2, "kind": "rules", "rules": []})


def test_parse_envelope_rejects_wrong_kind() -> None:
    with pytest.raises(UnsupportedExportVersion):
        parse_envelope({"kam_export_version": 1, "kind": "alert_history", "rules": []})


def test_parse_envelope_isolates_a_single_invalid_rule() -> None:
    envelope = {
        "kam_export_version": 1,
        "kind": "rules",
        "rules": [
            {"slug": "good", "alert_name": "Good", "expr": "up", "severity": "info"},
            {"slug": "bad-severity", "alert_name": "Bad", "expr": "up", "severity": "extreme"},
        ],
    }
    result = parse_envelope(envelope)
    good, bad = result.entries
    assert good.rule is not None and good.error is None
    assert bad.rule is None
    assert bad.slug == "bad-severity"
    assert bad.error is not None


# -- plan/execute: conflict strategies ---------------------------------


@respx.mock
async def test_no_conflict_is_always_created() -> None:
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing=set())
    k8s = _factory(fake_api)
    team = _team()
    cluster = _cluster()

    async with httpx.AsyncClient() as http_client:
        verdicts = await execute_import(
            k8s,
            http_client,
            team=team,
            target_cluster=cluster,
            entries=_entries(_rule(slug="new-rule")),
            conflict_strategy="skip",
        )

    assert [v.action for v in verdicts] == ["created"]
    assert len(fake_api.create_calls) == 1
    assert fake_api.create_calls[0]["metadata"]["name"] == "kam-t1-new-rule"
    assert not fake_api.replace_calls


@respx.mock
async def test_skip_strategy_leaves_existing_untouched() -> None:
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing={"kam-t1-high-cpu"})
    k8s = _factory(fake_api)

    async with httpx.AsyncClient() as http_client:
        verdicts = await execute_import(
            k8s,
            http_client,
            team=_team(),
            target_cluster=_cluster(),
            entries=_entries(_rule()),
            conflict_strategy="skip",
        )

    assert [v.action for v in verdicts] == ["skipped"]
    assert not fake_api.create_calls
    assert not fake_api.replace_calls


@respx.mock
async def test_overwrite_strategy_replaces_existing() -> None:
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing={"kam-t1-high-cpu"}, owner_team_id=1)
    k8s = _factory(fake_api)

    async with httpx.AsyncClient() as http_client:
        verdicts = await execute_import(
            k8s,
            http_client,
            team=_team(),
            target_cluster=_cluster(),
            entries=_entries(_rule()),
            conflict_strategy="overwrite",
        )

    assert [v.action for v in verdicts] == ["overwritten"]
    assert not fake_api.create_calls
    assert len(fake_api.replace_calls) == 1
    assert fake_api.replace_calls[0]["metadata"]["name"] == "kam-t1-high-cpu"


@respx.mock
async def test_rename_strategy_finds_first_free_suffix() -> None:
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing={"kam-t1-high-cpu"})
    k8s = _factory(fake_api)

    async with httpx.AsyncClient() as http_client:
        verdicts = await execute_import(
            k8s,
            http_client,
            team=_team(),
            target_cluster=_cluster(),
            entries=_entries(_rule()),
            conflict_strategy="rename",
        )

    assert [v.action for v in verdicts] == ["renamed"]
    assert verdicts[0].final_slug == "high-cpu-2"
    assert fake_api.create_calls[0]["metadata"]["name"] == "kam-t1-high-cpu-2"


@respx.mock
async def test_rename_strategy_skips_already_taken_suffix() -> None:
    """-2 is already occupied on the cluster -- the next free one is -3."""
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing={"kam-t1-high-cpu", "kam-t1-high-cpu-2"})
    k8s = _factory(fake_api)

    async with httpx.AsyncClient() as http_client:
        verdicts = await execute_import(
            k8s,
            http_client,
            team=_team(),
            target_cluster=_cluster(),
            entries=_entries(_rule()),
            conflict_strategy="rename",
        )

    assert verdicts[0].action == "renamed"
    assert verdicts[0].final_slug == "high-cpu-3"


@respx.mock
async def test_dry_run_makes_zero_writes_for_every_action() -> None:
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing={"kam-t1-existing"})
    k8s = _factory(fake_api)

    async with httpx.AsyncClient() as http_client:
        verdicts = await plan_import(
            k8s,
            http_client,
            team=_team(),
            target_cluster=_cluster(),
            entries=_entries(_rule(slug="new-one"), _rule(slug="existing")),
            conflict_strategy="rename",
        )

    actions = {v.slug: v.action for v in verdicts}
    assert actions == {"new-one": "created", "existing": "renamed"}
    assert not fake_api.create_calls
    assert not fake_api.replace_calls


@respx.mock
async def test_one_invalid_expr_does_not_abort_the_batch() -> None:
    def _validate(request: httpx.Request) -> httpx.Response:
        # form-urlencoded body ("query=<expr>") -- decode it properly rather
        # than substring-matching the raw (percent-encoded) bytes.
        expr = parse_qs(request.content.decode())["query"][0]
        return INVALID if "bad(" in expr else VALID

    respx.post(f"{PROM_URL}/api/v1/format_query").mock(side_effect=_validate)
    fake_api = FakeCoApi(existing=set())
    k8s = _factory(fake_api)

    async with httpx.AsyncClient() as http_client:
        verdicts = await execute_import(
            k8s,
            http_client,
            team=_team(),
            target_cluster=_cluster(),
            entries=_entries(_rule(slug="broken", expr="bad(("), _rule(slug="fine", expr="up")),
            conflict_strategy="skip",
        )

    by_slug = {v.slug: v for v in verdicts}
    assert by_slug["broken"].action == "failed"
    assert by_slug["broken"].errors
    assert by_slug["fine"].action == "created"
    assert len(fake_api.create_calls) == 1


@respx.mock
async def test_validate_is_called_against_the_target_clusters_prometheus() -> None:
    route = respx.post(f"{TARGET_PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing=set())
    k8s = _factory(fake_api)
    target = _cluster(cluster_id=2, name="staging-sim", prometheus_url=TARGET_PROM_URL)

    async with httpx.AsyncClient() as http_client:
        await execute_import(
            k8s,
            http_client,
            team=_team(),
            target_cluster=target,
            entries=_entries(_rule()),
            conflict_strategy="skip",
        )

    assert route.called


@respx.mock
async def test_prometheus_unavailable_raises_for_the_whole_batch() -> None:
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(side_effect=httpx.ConnectError("refused"))
    fake_api = FakeCoApi(existing=set())
    k8s = _factory(fake_api)

    async with httpx.AsyncClient() as http_client:
        with pytest.raises(PrometheusUnavailableError):
            await execute_import(
                k8s,
                http_client,
                team=_team(),
                target_cluster=_cluster(),
                entries=_entries(_rule()),
                conflict_strategy="skip",
            )

    assert not fake_api.create_calls


@respx.mock
async def test_retargeting_stamps_the_target_teams_labels_and_name() -> None:
    """A rule exported from one team, imported for a *different* team --
    build_prometheus_rule stamps the target team's own labels/name-prefix,
    regardless of whichever team the envelope originally came from (the
    envelope carries no ownership metadata to begin with)."""
    respx.post(f"{PROM_URL}/api/v1/format_query").mock(return_value=VALID)
    fake_api = FakeCoApi(existing=set())
    k8s = _factory(fake_api)
    target_team = _team(team_id=2, slug="payments")

    async with httpx.AsyncClient() as http_client:
        verdicts = await execute_import(
            k8s,
            http_client,
            team=target_team,
            target_cluster=_cluster(),
            entries=_entries(_rule(slug="high-cpu")),
            conflict_strategy="skip",
        )

    assert verdicts[0].action == "created"
    body = fake_api.create_calls[0]
    assert body["metadata"]["name"] == "kam-t2-high-cpu"
    assert body["metadata"]["labels"]["kam/team-id"] == "2"
    assert body["spec"]["groups"][0]["rules"][0]["labels"]["kam_team"] == "payments"
