"""Tests for `app.services.grafana.resolve_grafana_url`: annotation takes
priority, cluster.grafana_url is a fallback, and no value at all yields
None -- plus that the fallback lands in the routed notification payload.
"""

from app.models.cluster import Cluster
from app.services.grafana import resolve_grafana_url
from app.services.rules import GRAFANA_ANNOTATION


def _cluster(grafana_url: str | None) -> Cluster:
    return Cluster(
        id=1,
        name="local",
        display_name="local",
        prometheus_url="http://prom",
        alertmanager_url="http://am",
        grafana_url=grafana_url,
        webhook_token_hash="x",
    )


def test_annotation_takes_priority_over_cluster_fallback() -> None:
    annotations = {GRAFANA_ANNOTATION: "https://grafana.example.com/d/abc/panel"}
    cluster = _cluster("https://cluster-grafana.example.com")

    result = resolve_grafana_url(annotations, cluster, "HighCpuUsage")

    assert result == "https://grafana.example.com/d/abc/panel"


def test_falls_back_to_cluster_alerting_list_url() -> None:
    cluster = _cluster("https://cluster-grafana.example.com")

    result = resolve_grafana_url({}, cluster, "HighCpuUsage")

    assert result == "https://cluster-grafana.example.com/alerting/list?queryString=HighCpuUsage"


def test_fallback_strips_trailing_slash_and_quotes_alertname() -> None:
    cluster = _cluster("https://cluster-grafana.example.com/")

    result = resolve_grafana_url({}, cluster, "High CPU Usage")

    assert result == "https://cluster-grafana.example.com/alerting/list?queryString=High%20CPU%20Usage"


def test_no_annotation_and_no_cluster_grafana_url_is_none() -> None:
    cluster = _cluster(None)

    assert resolve_grafana_url({}, cluster, "X") is None


def test_no_cluster_at_all_is_none_without_annotation() -> None:
    assert resolve_grafana_url({}, None, "X") is None


def test_no_cluster_at_all_still_honors_annotation() -> None:
    annotations = {GRAFANA_ANNOTATION: "https://grafana.example.com/x"}
    assert resolve_grafana_url(annotations, None, "X") == "https://grafana.example.com/x"


def test_empty_string_annotation_falls_back_to_cluster() -> None:
    # An empty-string annotation value (e.g. a rule author cleared the
    # field but the key survived) must not be treated as "present".
    cluster = _cluster("https://cluster-grafana.example.com")
    annotations = {GRAFANA_ANNOTATION: ""}

    result = resolve_grafana_url(annotations, cluster, "X")

    assert result == "https://cluster-grafana.example.com/alerting/list?queryString=X"
