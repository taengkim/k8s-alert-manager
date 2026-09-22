import { apiFetch } from "./client";
import type { AdminUser, Cluster } from "./types";

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
