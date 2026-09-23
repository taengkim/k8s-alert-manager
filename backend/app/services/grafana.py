"""Grafana deep-link resolution for alert notifications and detail views.

Two sources, checked in priority order:

1. The alert's own `kam_grafana_url` annotation (`app.services.rules.
   GRAFANA_ANNOTATION`), set by a rule author via `RuleWrite.grafana_url` --
   always used as-is when present, since it's an operator-authored,
   alert-specific link (e.g. a specific dashboard panel already scoped to
   this alert).
2. A cluster-level fallback built from `cluster.grafana_url`, when no
   annotation is set.

Fallback URL format decision: Grafana's Explore deep-link format
(`/explore?schemaVersion=1&panes=...`) is both Grafana-version-sensitive and
datasource-UID-sensitive -- a URL built without knowing which datasource UID
a given Grafana instance uses for its Prometheus would 404 or silently open
an empty pane on any real deployment, and nothing in `Cluster` records that
UID. Rather than guess one (or require every cluster admin to additionally
configure a Grafana datasource UID just for link-building), the fallback
instead links into Grafana's built-in unified-alerting list, filtered by
alert name: `{grafana_url}/alerting/list?queryString={alertname}`. That
route and its `queryString` param have been stable since Grafana 8's
unified alerting GA through 11.x, so this deliberately trades link precision
(no guaranteed jump to a specific dashboard panel) for version independence
-- the one property that matters when `cluster.grafana_url` alone is all
this has to work with.
"""

from urllib.parse import quote

from app.models.cluster import Cluster
from app.services.rules import GRAFANA_ANNOTATION


def resolve_grafana_url(
    annotations: dict[str, str], cluster: Cluster | None, alertname: str
) -> str | None:
    """`annotations` is an event's (or a live alert's) annotation dict;
    `cluster` may be `None` when the owning cluster couldn't be resolved
    (defensive only -- every `AlertEvent`/live alert has a real cluster in
    practice), in which case only the annotation source applies.
    """
    from_annotation = annotations.get(GRAFANA_ANNOTATION)
    if from_annotation:
        return from_annotation

    if cluster is not None and cluster.grafana_url:
        base = cluster.grafana_url.rstrip("/")
        return f"{base}/alerting/list?queryString={quote(alertname)}"

    return None
