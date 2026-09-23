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

// -- Phase 15: retention settings -----------------------------------------

/** Effective retention windows (days), keyed by setting name -- always
 * reports every key (falls back to its server-side default when unset). */
export type RetentionSettings = Record<string, number>;

export function getRetentionSettings(): Promise<RetentionSettings> {
  return apiFetch<RetentionSettings>("/admin/settings");
}

export function updateRetentionSettings(
  values: Record<string, number>,
): Promise<RetentionSettings> {
  return apiFetch<RetentionSettings>("/admin/settings", { method: "PUT", body: { values } });
}

export interface RetentionPurgeSummary {
  alert_events: number;
  notification_outbox: number;
  scheduled_actions: number;
  audit_logs: number;
}

export function runRetentionPurge(): Promise<{ summary: RetentionPurgeSummary }> {
  return apiFetch<{ summary: RetentionPurgeSummary }>("/admin/retention/purge", {
    method: "POST",
  });
}
