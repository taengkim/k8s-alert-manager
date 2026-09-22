#!/usr/bin/env bash
# Idempotent bootstrap for the local dev kind cluster: kube-prometheus-stack
# wired for Alertmanager webhooks + seeded PrometheusRules.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLUSTER_NAME="kam"

cd "${REPO_ROOT}"

echo "==> Checking prerequisites"

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not reachable. Start Docker Desktop and retry." >&2
  exit 1
fi

if ! command -v kind >/dev/null 2>&1; then
  echo "ERROR: 'kind' is not installed. Install it with: brew install kind" >&2
  exit 1
fi

if ! command -v helm >/dev/null 2>&1; then
  echo "ERROR: 'helm' is not installed." >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: 'kubectl' is not installed." >&2
  exit 1
fi

echo "==> Ensuring kind cluster '${CLUSTER_NAME}' exists"
if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  echo "    cluster '${CLUSTER_NAME}' already exists, skipping create"
else
  kind create cluster --name "${CLUSTER_NAME}" --config dev/kind-config.yaml
fi

echo "==> Ensuring prometheus-community helm repo"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update >/dev/null

echo "==> Installing/upgrading kube-prometheus-stack (this can take 5-10 minutes)"
helm upgrade --install kps prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f dev/kube-prometheus-values.yaml \
  --wait --timeout 10m

echo "==> Ensuring kam-rules namespace"
kubectl create namespace kam-rules --dry-run=client -o yaml | kubectl apply -f -

echo "==> Applying seed PrometheusRules"
kubectl apply -f dev/seed/always-firing-rule.yaml

cat <<EOF

==> Dev cluster ready
    Prometheus:   http://localhost:30090
    Alertmanager: http://localhost:30093

Note: Alertmanager webhook delivery to http://host.docker.internal:8000 will
fail (connection refused / 404) until the webhook endpoint is implemented
(Phase 7) and the backend is running on the host. That is expected for now.
EOF
