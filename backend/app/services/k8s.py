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
import os
import tempfile
from typing import Any

import yaml
from kubernetes import client, config
from kubernetes.client import ApiClient, CoreV1Api, CustomObjectsApi, VersionApi
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
    """Raised when the k8s API server can't be reached (connect failure,
    5xx), or its credentials/config can't even be built."""


class K8sBadRequestError(Exception):
    """Raised for a 400/405/415/422 from the k8s API server -- something
    about *our own request* was malformed, as opposed to the cluster being
    unreachable or rejecting us (401/403/429 and everything else are
    K8sUnavailableError instead). The API layer maps this to 422,
    surfacing the k8s message since it describes the caller's own input."""


class RuleConflictError(Exception):
    """Raised when creating a PrometheusRule whose name already exists."""


class RuleUpdateConflictError(Exception):
    """Raised when a replace/delete's resourceVersion precondition fails
    (409): someone else modified or deleted the rule between our read and
    write."""


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


# Only these statuses mean "our own request was malformed" -- everything
# else (401/403/429 included) reflects something wrong with reaching or
# using the cluster, not a bad request shape we control.
_BAD_REQUEST_STATUSES = {400, 405, 415, 422}


def _api_exception_detail(exc: ApiException) -> str:
    """A safe, client-facing detail string for an ApiException.

    Never use `str(exc)` for anything that reaches a log line or an API
    response: the kubernetes client's ApiException.__str__ embeds the full
    HTTP response -- every response header plus the raw body -- which can
    include things we don't want to hand back to a caller (e.g. our own
    ServiceAccount's identity showing up in a 403's body). This surfaces
    only the HTTP reason phrase plus the k8s API server's own JSON body
    `message` field, if present.
    """
    reason = exc.reason or "error"
    message = None
    if exc.body:
        try:
            data = json.loads(exc.body)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            candidate = data.get("message")
            if isinstance(candidate, str):
                message = candidate
    return f"{reason}: {message}" if message else reason


def _map_status(exc: ApiException) -> Exception:
    """Map an ApiException that has no caller-specific handling for its
    status code (404/409 are always handled by the caller first) to a typed
    error: 400/405/415/422 are our own bad request; anything else (401,
    403, 429, 5xx, or no status at all) means the cluster itself is
    unavailable -- including "unavailable to us" cases like an auth/rate-
    limit rejection, not just connect failures."""
    status_code = exc.status or 0
    detail = _api_exception_detail(exc)
    if status_code in _BAD_REQUEST_STATUSES:
        return K8sBadRequestError(detail)
    return K8sUnavailableError(detail)


class K8sClientFactory:
    """Caches per-cluster `ApiClient`s, invalidated when the cluster row
    changes.

    Cache key is `(cluster.id, cluster.updated_at)` so an edit to a
    cluster's credentials/auth kind naturally evicts the stale client on the
    next `get()` -- editing `updated_at` is enough, no explicit invalidation
    call needed. Each cache entry also carries the path of any CA-cert temp
    file written for it (token auth kind only), so it can be removed once
    the entry is evicted rather than accumulating forever.
    """

    def __init__(self) -> None:
        self._cache: dict[int, tuple[Any, ApiClient, str | None]] = {}

    def get(self, cluster: Cluster) -> ApiClient:
        cached = self._cache.get(cluster.id)
        if cached is not None and cached[0] == cluster.updated_at:
            return cached[1]

        if cached is not None:
            self._cleanup_ca_file(cached[2])

        api_client, ca_path = self._build(cluster)
        self._cache[cluster.id] = (cluster.updated_at, api_client, ca_path)
        return api_client

    @staticmethod
    def _cleanup_ca_file(path: str | None) -> None:
        if path is None:
            return
        try:
            os.unlink(path)
        except OSError:
            pass

    def _build(self, cluster: Cluster) -> tuple[ApiClient, str | None]:
        """Build a client for `cluster`, containing any credential/config
        failure to a generic K8sUnavailableError.

        This is deliberately paranoid: decrypting/parsing a cluster's stored
        credentials can fail in several library-specific ways (bad Fernet
        token, invalid YAML/JSON, a malformed kubeconfig dict rejected by
        the kubernetes client's own config loader, ...), and every one of
        those exceptions' messages can embed a fragment of the *decrypted*
        secret. None of that text may ever reach a log line or an API
        response -- only the cluster name and the exception's class name are
        logged, at most.
        """
        try:
            return self._build_unsafe(cluster)
        except K8sUnavailableError:
            # Already a deliberate, safe-to-surface message (e.g. "unknown
            # auth kind") -- pass it through as-is rather than re-wrapping.
            raise
        except Exception as exc:
            # Broad on purpose: covers cryptography.fernet.InvalidToken,
            # yaml.YAMLError, json.JSONDecodeError, KeyError (missing
            # expected field in decrypted JSON), and the kubernetes client's
            # own config.ConfigException, among others -- all of which can
            # carry decrypted credential material in their message.
            logger.warning(
                "failed to build k8s client for cluster '%s': %s",
                cluster.name,
                type(exc).__name__,
            )
            raise K8sUnavailableError(
                f"cluster '{cluster.name}' credentials/config invalid"
            ) from exc

    def _build_unsafe(self, cluster: Cluster) -> tuple[ApiClient, str | None]:
        if cluster.k8s_auth_kind == "incluster":
            config.load_incluster_config()
            return ApiClient(), None

        if cluster.k8s_auth_kind == "kubeconfig":
            if cluster.credentials_encrypted:
                kubeconfig_dict = yaml.safe_load(decrypt_str(cluster.credentials_encrypted))
                return config.new_client_from_config_dict(kubeconfig_dict), None
            # Dev default: no stored credentials means "use whatever the
            # host's default kubeconfig/current-context points at".
            return config.new_client_from_config(), None

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
            ca_path: str | None = None
            if ca_cert:
                ca_path = _write_ca_cert(ca_cert)
                configuration.ssl_ca_cert = ca_path
            else:
                # No CA provided: accept an unverified TLS connection rather
                # than fail closed. This is a deliberate dev/self-signed-
                # cluster accommodation -- clusters with a real CA should
                # always supply ca_cert.
                configuration.verify_ssl = False
            return ApiClient(configuration), ca_path

        raise K8sUnavailableError(
            f"unknown k8s_auth_kind '{cluster.k8s_auth_kind}' for cluster '{cluster.name}'"
        )

    # -- test seams -------------------------------------------------------

    def _co_api(self, cluster: Cluster) -> CustomObjectsApi:
        return CustomObjectsApi(self.get(cluster))

    def _core_api(self, cluster: Cluster) -> CoreV1Api:
        return CoreV1Api(self.get(cluster))

    def _version_api(self, cluster: Cluster) -> VersionApi:
        """Test seam for `app.services.cluster_health`'s k8s reachability
        check -- mirrors `_co_api`/`_core_api` above.
        """
        return VersionApi(self.get(cluster))

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
                if exc.status == 404:
                    # A 404 here means the *list endpoint itself* doesn't
                    # exist -- almost always the PrometheusRule CRD isn't
                    # installed on this cluster (no Prometheus Operator).
                    # That's a cluster-level problem, not a bad request.
                    raise K8sUnavailableError(
                        f"PrometheusRule CRD not found on cluster '{cluster.name}' "
                        "(is the Prometheus Operator installed?)"
                    ) from exc
                raise _map_status(exc) from exc
            except Exception as exc:
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
                raise _map_status(exc) from exc
            except Exception as exc:
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
                raise _map_status(exc) from exc
            except Exception as exc:
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

        try:
            resource_version = existing["metadata"]["resourceVersion"]
        except KeyError as exc:
            raise K8sUnavailableError(
                f"rule '{name}' has no resourceVersion; refusing to replace"
            ) from exc

        # Copy rather than mutate the caller's body: build_prometheus_rule's
        # manifest is only ever fed to one call site today, but this
        # function has no business rewriting a dict handed to it by
        # someone else.
        body = {**body, "metadata": {**body.get("metadata", {}), "resourceVersion": resource_version}}

        def _call() -> dict[str, Any]:
            api = self._co_api(cluster)
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
                if exc.status == 409:
                    # The resourceVersion precondition failed: someone else
                    # wrote this rule between our read and this write.
                    raise RuleUpdateConflictError(
                        f"rule '{name}' was modified concurrently"
                    ) from exc
                raise _map_status(exc) from exc
            except Exception as exc:
                raise K8sUnavailableError(str(exc)) from exc

        return await asyncio.to_thread(_call)

    async def delete_rule(self, cluster: Cluster, name: str, team_id: int) -> None:
        existing = await self.get_rule(cluster, name)
        if not _is_owned_by(existing, team_id):
            raise RuleForbiddenError(
                f"rule '{name}' is not managed by kam for this team"
            )

        try:
            resource_version = existing["metadata"]["resourceVersion"]
        except KeyError as exc:
            raise K8sUnavailableError(
                f"rule '{name}' has no resourceVersion; refusing to delete"
            ) from exc

        def _call() -> None:
            api = self._co_api(cluster)
            delete_options = client.V1DeleteOptions(
                preconditions=client.V1Preconditions(resource_version=resource_version)
            )
            try:
                api.delete_namespaced_custom_object(
                    group=RULE_GROUP,
                    version=RULE_VERSION,
                    namespace=cluster.rules_namespace,
                    plural=RULE_PLURAL,
                    name=name,
                    body=delete_options,
                )
            except ApiException as exc:
                if exc.status == 409:
                    # Precondition failed: the rule was modified (its
                    # resourceVersion moved on) between our read and this
                    # delete.
                    raise RuleUpdateConflictError(
                        f"rule '{name}' was modified concurrently"
                    ) from exc
                if exc.status == 404:
                    # Already gone -- e.g. a concurrent delete beat us to
                    # it. The desired end state (rule absent) is achieved.
                    return
                raise _map_status(exc) from exc
            except Exception as exc:
                raise K8sUnavailableError(str(exc)) from exc

        await asyncio.to_thread(_call)

    async def list_namespaces(self, cluster: Cluster) -> list[str]:
        def _call() -> list[str]:
            api = self._core_api(cluster)
            try:
                result = api.list_namespace()
            except ApiException as exc:
                raise _map_status(exc) from exc
            except Exception as exc:
                raise K8sUnavailableError(str(exc)) from exc
            return [item.metadata.name for item in result.items]

        return await asyncio.to_thread(_call)


def _write_ca_cert(ca_cert_pem: str) -> str:
    """Write a token-auth cluster's CA cert to a fresh, freshly-created
    0600 temp file (the k8s client wants a file path, not raw PEM).

    Uses `mkstemp` rather than `NamedTemporaryFile` so the file is created
    with owner-only permissions atomically at creation time, and the path
    is unpredictable (no cluster id embedded in it). The caller
    (`K8sClientFactory`) owns removing this file when its cache entry is
    evicted.
    """
    fd, path = tempfile.mkstemp(prefix="kam-ca-", suffix=".pem")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(ca_cert_pem)
    except BaseException:
        os.unlink(path)
        raise
    return path
