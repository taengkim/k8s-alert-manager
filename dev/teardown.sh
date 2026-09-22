#!/usr/bin/env bash
# Tear down the local dev kind cluster. Safe to run even if it doesn't exist.
set -euo pipefail

CLUSTER_NAME="kam"

if kind get clusters 2>/dev/null | grep -qx "${CLUSTER_NAME}"; then
  kind delete cluster --name "${CLUSTER_NAME}"
else
  echo "==> kind cluster '${CLUSTER_NAME}' not found, nothing to do"
fi
