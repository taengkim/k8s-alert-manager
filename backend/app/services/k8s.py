"""Kubernetes client factory skeleton.

Phase 4 implements real client construction (incluster / kubeconfig / token
auth kinds, decrypting `Cluster.credentials_encrypted` as needed). For now
this is just a placeholder so callers/tests can wire against a stable
interface without a `kubernetes` client dependency yet.
"""

from typing import Any


class K8sClientFactory:
    """Caches per-cluster clients, invalidated when the cluster row changes.

    Cache key is `(cluster.id, cluster.updated_at)` so an edit to a cluster's
    credentials/auth kind naturally evicts the stale client on next `get()`.
    """

    def __init__(self) -> None:
        self._cache: dict[tuple[int, Any], Any] = {}

    def get(self, cluster: Any) -> Any:
        raise NotImplementedError("K8sClientFactory.get is implemented in Phase 4")
