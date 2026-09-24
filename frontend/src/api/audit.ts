import { apiFetch } from "./client";

export interface AuditLogEntry {
  id: number;
  created_at: string;
  user_id: number | null;
  username: string | null;
  team_id: number | null;
  action: string;
  object_type: string;
  object_ref: string;
  detail: Record<string, unknown> | null;
}

export interface AuditLogResponse {
  items: AuditLogEntry[];
  total: number;
  page: number;
  page_size: number;
}

export interface AuditLogFilters {
  teamId?: number;
  userId?: number;
  /** Prefix match server-side -- "rule." matches every rule.* action. */
  action?: string;
  fromTs?: string;
  toTs?: string;
  page?: number;
  pageSize?: number;
}

export function getAuditLogs(filters: AuditLogFilters): Promise<AuditLogResponse> {
  const params = new URLSearchParams();
  if (filters.teamId !== undefined) params.set("team_id", String(filters.teamId));
  if (filters.userId !== undefined) params.set("user_id", String(filters.userId));
  if (filters.action) params.set("action", filters.action);
  if (filters.fromTs) params.set("from_ts", filters.fromTs);
  if (filters.toTs) params.set("to_ts", filters.toTs);
  params.set("page", String(filters.page ?? 1));
  params.set("page_size", String(filters.pageSize ?? 50));

  return apiFetch<AuditLogResponse>(`/audit?${params.toString()}`);
}
