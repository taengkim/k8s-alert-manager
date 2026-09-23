import { apiFetch } from "./client";

export type AlertEventStatus = "firing" | "resolved";

export interface AlertEventSummary {
  id: number;
  cluster_id: number;
  cluster_name: string;
  fingerprint: string;
  status: AlertEventStatus;
  alertname: string;
  severity: string | null;
  namespace: string | null;
  team_id: number | null;
  starts_at: string;
  ends_at: string | null;
  first_received_at: string;
  last_received_at: string;
  receive_count: number;
}

export interface AlertEventDetail extends AlertEventSummary {
  labels: Record<string, string>;
  annotations: Record<string, string>;
  generator_url: string | null;
}

export interface AlertHistoryResponse {
  items: AlertEventSummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface AlertHistoryFilters {
  teamId?: number;
  clusterIds?: number[];
  status?: AlertEventStatus;
  /** "none" matches events with no severity label at all. */
  severity?: string[];
  namespace?: string;
  search?: string;
  fromTs?: string;
  toTs?: string;
  page?: number;
  pageSize?: number;
}

export function getAlertHistory(filters: AlertHistoryFilters): Promise<AlertHistoryResponse> {
  const params = new URLSearchParams();
  if (filters.teamId !== undefined) params.set("team_id", String(filters.teamId));
  for (const id of filters.clusterIds ?? []) params.append("cluster_id", String(id));
  if (filters.status) params.set("status", filters.status);
  if (filters.severity && filters.severity.length > 0) {
    params.set("severity", filters.severity.join(","));
  }
  if (filters.namespace) params.set("namespace", filters.namespace);
  if (filters.search) params.set("search", filters.search);
  if (filters.fromTs) params.set("from_ts", filters.fromTs);
  if (filters.toTs) params.set("to_ts", filters.toTs);
  params.set("page", String(filters.page ?? 1));
  params.set("page_size", String(filters.pageSize ?? 50));

  return apiFetch<AlertHistoryResponse>(`/alerts/history?${params.toString()}`);
}

export function getAlertHistoryDetail(eventId: number): Promise<AlertEventDetail> {
  return apiFetch<AlertEventDetail>(`/alerts/history/${eventId}`);
}
