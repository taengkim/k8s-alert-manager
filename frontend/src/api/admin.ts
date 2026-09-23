import { apiFetch } from "./client";
import type { AdminUser, Cluster, ClusterHealth, ClusterSecretReveal, ClusterWriteInput } from "./types";

export function listUsers(): Promise<AdminUser[]> {
  return apiFetch<AdminUser[]>("/admin/users");
}

export function patchUser(
  userId: number,
  body: { is_admin?: boolean; is_active?: boolean },
): Promise<AdminUser> {
  return apiFetch<AdminUser>(`/admin/users/${userId}`, { method: "PATCH", body });
}

export function listClusters(): Promise<Cluster[]> {
  return apiFetch<Cluster[]>("/clusters");
}

export function createCluster(body: ClusterWriteInput): Promise<Cluster & ClusterSecretReveal> {
  return apiFetch<Cluster & ClusterSecretReveal>("/clusters", { method: "POST", body });
}

export function updateCluster(
  clusterId: number,
  body: Partial<ClusterWriteInput>,
): Promise<Cluster & Partial<ClusterSecretReveal>> {
  return apiFetch<Cluster & Partial<ClusterSecretReveal>>(`/clusters/${clusterId}`, {
    method: "PATCH",
    body,
  });
}

export function deleteCluster(clusterId: number): Promise<void> {
  return apiFetch<void>(`/clusters/${clusterId}`, { method: "DELETE" });
}

export function getClusterHealth(clusterId: number, refresh = false): Promise<ClusterHealth> {
  return apiFetch<ClusterHealth>(`/clusters/${clusterId}/health${refresh ? "?refresh=true" : ""}`);
}
