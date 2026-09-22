"""Kubernetes client factory + PrometheusRule CRUD operations.

The official `kubernetes` client is synchronous, so every call that touches
it is wrapped in `asyncio.to_thread` here -- callers never block the event
loop on a k8s API round trip.

`_co_api`/`_core_api` are the test seam: unit tests monkeypatch these on a
`K8sClientFactory` instance to hand back a fake `CustomObjectsApi`/
`CoreV1Api` without touching a real cluster.
"""

import asyncio
import json
import logging
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client import ApiClient, CoreV1Api, CustomObjectsApi
from kubernetes.client.exceptions import ApiException

from app.models.cluster import Cluster
from app.security import decrypt_str

logger = logging.getLogger(__name__)

RULE_GROUP = "monitoring.coreos.com"
RULE_VERSION = "v1"
RULE_PLURAL = "prometheusrules"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
TEAM_ID_LABEL = "kam/team-id"
MANAGED_BY_VALUE = "kam"


class K8sUnavailableError(Exception):
    """Raised when the k8s API server can't be reached, or returns an
    unexpected error we can't attribute to conflict/ownership."""


class RuleConflictError(Exception):
    """Raised when creating a PrometheusRule whose name already exists."""


class RuleForbiddenError(Exception):
    """Raised when an operation would mutate a PrometheusRule that isn't
    managed by kam, or is managed by kam for a different team.

    This is the server-side ownership guard: it is enforced here in the
    service layer (not just checked by the API layer) so a bug or bypass in
    routing/RBAC code can never result in a foreign or unmanaged rule being
    mutated.
    """


def _label_selector(team_id: int) -> str:
    return f"{MANAGED_BY_LABEL}={MANAGED_BY_VALUE},{TEAM_ID_LABEL}={team_id}"


def _is_owned_by(obj: dict[str, Any] | None, team_id: int) -> bool:
    labels = ((obj or {}).get("metadata") or {}).get("labels") or {}
    return (
        labels.get(MANAGED_BY_LABEL) == MANAGED_BY_VALUE
        and labels.get(TEAM_ID_LABEL) == str(team_id)
    )


class K8sClientFactory:
    """Caches per-cluster `ApiClient`s, invalidated when the cluster row
    changes.

    Cache key is `(cluster.id, cluster.updated_at)` so an edit to a
    cluster's credentials/auth kind naturally evicts the stale client on the
    next `get()` -- editing `updated_at` is enough, no explicit invalidation
    call needed.
    """

    def __init__(self) -> None:
        self._cache: dict[int, tuple[Any, ApiClient]] = {}

    def get(self, cluster: Cluster) -> ApiClient:
        cached = self._cache.get(cluster.id)
        if cached is not None and cached[0] == cluster.updated_at:
            return cached[1]

        api_client = self._build(cluster)
        self._cache[cluster.id] = (cluster.updated_at, api_client)
        return api_client

    def _build(self, cluster: Cluster) -> ApiClient:
        if cluster.k8s_auth_kind == "incluster":
            config.load_incluster_config()
            return ApiClient()

        if cluster.k8s_auth_kind == "kubeconfig":
            if cluster.credentials_encrypted:
                kubeconfig_dict = yaml.safe_load(decrypt_str(cluster.credentials_encrypted))
                return config.new_client_from_config_dict(kubeconfig_dict)
            # Dev default: no stored credentials means "use whatever the
            # host's default kubeconfig/current-context points at".
            return config.new_client_from_config()

        if cluster.k8s_auth_kind == "token":
            if not cluster.credentials_encrypted:
                raise K8sUnavailableError(
                    f"cluster '{cluster.name}' has auth kind 'token' but no credentials stored"
                )
            creds = json.loads(decrypt_str(cluster.credentials_encrypted))
            configuration = client.Configuration(host=cluster.k8s_api_url)
            configuration.api_key_prefix["authorization"] = "Bearer"
            configuration.api_key["authorization"] = creds["token"]
            ca_cert = creds.get("ca_cert")
            if ca_cert:
                configuration.ssl_ca_cert = _write_ca_cert(cluster.id, ca_cert)
            else:
                # No CA provided: accept an unverified TLS connection rather
                # than fail closed. This is a deliberate dev/self-signed-
                # cluster accommodation -- clusters with a real CA should
                # always supply ca_cert.
                configuration.verify_ssl = False
            return ApiClient(configuration)

        raise K8sUnavailableError(
            f"unknown k8s_auth_kind '{cluster.k8s_auth_kind}' for cluster '{cluster.name}'"
        )

    # -- test seams -------------------------------------------------------

    def _co_api(self, cluster: Cluster) -> CustomObjectsApi:
        return CustomObjectsApi(self.get(cluster))

    def _core_api(self, cluster: Cluster) -> CoreV1Api:
        return CoreV1Api(self.get(cluster))

    # -- rule operations ----------------------------------------------------

    async def list_rules(self, cluster: Cluster, team_id: int) -> list[dict[str, Any]]:
        def _call() -> list[dict[str, Any]]:
            api = self._co_api(cluster)
            try:
                result = api.list_namespaced_custom_object(
                    group=RULE_GROUP,
                    version=RULE_VERSION,
                    namespace=cluster.rules_namespace,
                    plural=RULE_PLURAL,
                    label_selector=_label_selector(team_id),
                )
            except ApiException as exc:
                raise K8sUnavailableError(str(exc)) from exc
            return result.get("items", [])

        return await asyncio.to_thread(_call)

    async def get_rule(self, cluster: Cluster, name: str) -> dict[str, Any] | None:
        def _call() -> dict[str, Any] | None:
            api = self._co_api(cluster)
            try:
                return api.get_namespaced_custom_object(
                    group=RULE_GROUP,
                    version=RULE_VERSION,
                    namespace=cluster.rules_namespace,
                    plural=RULE_PLURAL,
                    name=name,
                )
            except ApiException as exc:
                if exc.status == 404:
                    return None
                raise K8sUnavailableError(str(exc)) from exc

        return await asyncio.to_thread(_call)

    async def create_rule(self, cluster: Cluster, body: dict[str, Any]) -> dict[str, Any]:
        def _call() -> dict[str, Any]:
            api = self._co_api(cluster)
            try:
                return api.create_namespaced_custom_object(
                    group=RULE_GROUP,
                    version=RULE_VERSION,
                    namespace=cluster.rules_namespace,
                    plural=RULE_PLURAL,
                    body=body,
                )
            except ApiException as exc:
                if exc.status == 409:
                    name = body.get("metadata", {}).get("name", "?")
                    raise RuleConflictError(f"rule '{name}' already exists") from exc
                raise K8sUnavailableError(str(exc)) from exc

        return await asyncio.to_thread(_call)

    async def replace_rule(
        self, cluster: Cluster, name: str, team_id: int, body: dict[str, Any]
    ) -> dict[str, Any]:
        existing = await self.get_rule(cluster, name)
        if not _is_owned_by(existing, team_id):
            raise RuleForbiddenError(
                f"rule '{name}' is not managed by kam for this team"
            )

        def _call() -> dict[str, Any]:
            api = self._co_api(cluster)
            # Carry over resourceVersion for optimistic-concurrency; without
            # it the API server rejects the replace as a conflict.
            body.setdefault("metadata", {})["resourceVersion"] = existing["metadata"][
                "resourceVersion"
            ]
            try:
                return api.replace_namespaced_custom_object(
                    group=RULE_GROUP,
                    version=RULE_VERSION,
                    namespace=cluster.rules_namespace,
                    plural=RULE_PLURAL,
                    name=name,
                    body=body,
                )
            except ApiException as exc:
                raise K8sUnavailableError(str(exc)) from exc

        return await asyncio.to_thread(_call)

    async def delete_rule(self, cluster: Cluster, name: str, team_id: int) -> None:
        existing = await self.get_rule(cluster, name)
        if not _is_owned_by(existing, team_id):
            raise RuleForbiddenError(
                f"rule '{name}' is not managed by kam for this team"
            )

        def _call() -> None:
            api = self._co_api(cluster)
            try:
                api.delete_namespaced_custom_object(
                    group=RULE_GROUP,
                    version=RULE_VERSION,
                    namespace=cluster.rules_namespace,
                    plural=RULE_PLURAL,
                    name=name,
                )
            except ApiException as exc:
                raise K8sUnavailableError(str(exc)) from exc

        await asyncio.to_thread(_call)

    async def list_namespaces(self, cluster: Cluster) -> list[str]:
        def _call() -> list[str]:
            api = self._core_api(cluster)
            try:
                result = api.list_namespace()
            except ApiException as exc:
                raise K8sUnavailableError(str(exc)) from exc
            return [item.metadata.name for item in result.items]

        return await asyncio.to_thread(_call)


def _write_ca_cert(cluster_id: int, ca_cert_pem: str) -> str:
    """Persist a token-auth cluster's CA cert to a stable per-cluster path so
    `Configuration.ssl_ca_cert` (which wants a file path, not raw PEM) has
    something to read.
    """
    import tempfile
    from pathlib import Path

    path = Path(tempfile.gettempdir()) / f"kam-cluster-{cluster_id}-ca.pem"
    path.write_text(ca_cert_pem)
    return str(path)
