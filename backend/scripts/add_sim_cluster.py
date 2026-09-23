"""Idempotent dev helper: registers a second cluster row, `staging-sim`,
pointed at the *same* kind cluster's Prometheus/Alertmanager/kubeconfig as
the seeded `local` cluster.

This is a same-infrastructure simulation, not a real second cluster (a real
`dev-up-2` kind cluster is out of scope for this phase -- see README) --
it exists so multi-cluster UI/API paths (ClusterFilter, the admin clusters
page, per-cluster webhook auth, routing rules scoped to one cluster) have a
second row to exercise locally without standing up real infrastructure.
Alerts posted to `staging-sim`'s webhook token are indistinguishable, from
Prometheus/Alertmanager's point of view, from ones posted to `local`'s --
only kam's own bookkeeping treats them as two different clusters.

Run via `make dev-second-cluster-sim` (or `cd backend && uv run python -m
scripts.add_sim_cluster`). Requires the backend API to be running
(`make backend`) and reachable at `KAM_API_BASE_URL` (default
http://localhost:8000), and the dev LDAP/seed-dev admin account to exist
(`make seed-dev`) -- it authenticates as that account to call the
admin-only cluster-create API, exactly like a human admin would from
/admin/clusters, rather than writing to the database directly.

Safe to re-run: if `staging-sim` already exists, this prints its id and
exits without creating a duplicate (the webhook token itself, being visible
only once at creation time, can't be printed again on a later run -- use
the admin UI's "웹훅 토큰 회전" if it's been lost).
"""

import asyncio
import os
import sys

import httpx

API_BASE_URL = os.environ.get("KAM_API_BASE_URL", "http://localhost:8000")
ADMIN_USERNAME = os.environ.get("KAM_SIM_ADMIN_USERNAME", "alice")
ADMIN_PASSWORD = os.environ.get("KAM_SIM_ADMIN_PASSWORD", "password")
SIM_CLUSTER_NAME = "staging-sim"
SIM_DISPLAY_NAME = "Staging (sim)"


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    if response.status_code != 200:
        print(
            f"ERROR: login as '{ADMIN_USERNAME}' failed ({response.status_code}): "
            f"{response.text}\nHas `make seed-dev` been run, and is LDAP "
            "(`make dev-deps`) up?",
            file=sys.stderr,
        )
        sys.exit(1)


async def _find_existing(client: httpx.AsyncClient) -> dict | None:
    response = await client.get("/api/v1/clusters")
    response.raise_for_status()
    for cluster in response.json():
        if cluster["name"] == SIM_CLUSTER_NAME:
            return cluster
    return None


async def _default_cluster(client: httpx.AsyncClient) -> dict:
    response = await client.get("/api/v1/clusters")
    response.raise_for_status()
    for cluster in response.json():
        if cluster["name"] == "local":
            return cluster
    print(
        "ERROR: no 'local' cluster found -- the backend must have run its "
        "startup seed (ensure_default_cluster) at least once.",
        file=sys.stderr,
    )
    sys.exit(1)


async def main() -> None:
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=10.0) as client:
        await _login(client)

        existing = await _find_existing(client)
        if existing is not None:
            print(
                f"'{SIM_CLUSTER_NAME}' already exists (id={existing['id']}) -- skipping.\n"
                "Its webhook token was only ever shown once, at creation; use the "
                "admin clusters page's '웹훅 토큰 회전' if you need a fresh one."
            )
            return

        default_cluster = await _default_cluster(client)

        response = await client.post(
            "/api/v1/clusters",
            json={
                "name": SIM_CLUSTER_NAME,
                "display_name": SIM_DISPLAY_NAME,
                "k8s_auth_kind": "kubeconfig",
                "prometheus_url": default_cluster["prometheus_url"],
                "alertmanager_url": default_cluster["alertmanager_url"],
                "grafana_url": default_cluster.get("grafana_url"),
                "rules_namespace": default_cluster["rules_namespace"],
            },
        )
        if response.status_code != 201:
            print(
                f"ERROR: cluster create failed ({response.status_code}): {response.text}",
                file=sys.stderr,
            )
            sys.exit(1)

        body = response.json()
        print(f"Created cluster '{SIM_CLUSTER_NAME}' (id={body['id']}).\n")
        print(f"Webhook token (shown once): {body['webhook_token']}\n")
        print("Alertmanager receiver config snippet:")
        print(body["am_config_snippet"])
        print(
            "This cluster shares the same Prometheus/Alertmanager/kubeconfig as "
            "'local' -- it's a same-infrastructure simulation for exercising "
            "multi-cluster UI/API paths, not a real second cluster. Post a test "
            "alert with this token directly to see it show up as a distinct "
            "cluster in the app:\n\n"
            f"  curl -X POST {API_BASE_URL}/api/v1/webhook/alertmanager \\\n"
            f'    -H "Authorization: Bearer {body["webhook_token"]}" \\\n'
            '    -H "Content-Type: application/json" \\\n'
            '    -d \'{"version":"4","groupKey":"{}:{}","status":"firing","alerts":'
            '[{"status":"firing","labels":{"alertname":"SimAlert",'
            '"kam_team":"platform","severity":"warning"},"annotations":{},'
            '"startsAt":"2026-01-01T00:00:00Z","fingerprint":"sim-fp-1"}]}\''
        )


if __name__ == "__main__":
    asyncio.run(main())
