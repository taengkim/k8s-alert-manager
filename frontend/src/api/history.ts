import { apiFetch, downloadFile } from "./client";

export type AlertEventStatus = "firing" | "resolved";

export interface UserRef {
  id: number;
  username: string;
}

export interface CommentUserRef extends UserRef {
  display_name: string;
}

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
  is_test: boolean;
  acknowledged_at: string | null;
  acknowledged_by: UserRef | null;
  assignee: UserRef | null;
  /** Phase 14: the owner team's slug when this event reached the viewer
   * only via an AlertShare, null for the viewer's own team's events. */
  shared_from: string | null;
}

export interface AlertEventDetail extends AlertEventSummary {
  labels: Record<string, string>;
  annotations: Record<string, string>;
  generator_url: string | null;
  grafana_url: string | null;
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
  /** Defaults to false server-side -- excludes POST .../test-alert rows. */
  includeTest?: boolean;
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
  if (filters.includeTest) params.set("include_test", "true");
  params.set("page", String(filters.page ?? 1));
  params.set("page_size", String(filters.pageSize ?? 50));

  return apiFetch<AlertHistoryResponse>(`/alerts/history?${params.toString()}`);
}

export function ackAlert(eventId: number): Promise<AlertEventDetail> {
  return apiFetch<AlertEventDetail>(`/alerts/history/${eventId}/ack`, { method: "POST" });
}

export function unackAlert(eventId: number): Promise<AlertEventDetail> {
  return apiFetch<AlertEventDetail>(`/alerts/history/${eventId}/ack`, { method: "DELETE" });
}

export function setAlertAssignee(
  eventId: number,
  userId: number | null,
): Promise<AlertEventDetail> {
  return apiFetch<AlertEventDetail>(`/alerts/history/${eventId}/assignee`, {
    method: "PUT",
    body: { user_id: userId },
  });
}

export function resolveTestAlert(eventId: number): Promise<AlertEventDetail> {
  return apiFetch<AlertEventDetail>(`/alerts/history/${eventId}/resolve-test`, {
    method: "POST",
  });
}

export interface AlertComment {
  id: number;
  user: CommentUserRef | null;
  body: string;
  created_at: string;
}

export function getAlertComments(eventId: number): Promise<AlertComment[]> {
  return apiFetch<AlertComment[]>(`/alerts/history/${eventId}/comments`);
}

export function addAlertComment(eventId: number, body: string): Promise<AlertComment> {
  return apiFetch<AlertComment>(`/alerts/history/${eventId}/comments`, {
    method: "POST",
    body: { body },
  });
}

export function deleteAlertComment(commentId: number): Promise<void> {
  return apiFetch<void>(`/comments/${commentId}`, { method: "DELETE" });
}

export function getAlertHistoryDetail(eventId: number): Promise<AlertEventDetail> {
  return apiFetch<AlertEventDetail>(`/alerts/history/${eventId}`);
}

export type NotificationStatus = "pending" | "in_progress" | "delivered" | "failed" | "dead";

export interface AlertNotificationRecord {
  id: number;
  channel_id: number;
  channel_name: string;
  trigger: "firing" | "resolved";
  status: NotificationStatus;
  attempts: number;
  last_error: string | null;
  created_at: string;
  delivered_at: string | null;
}

export function getAlertHistoryNotifications(eventId: number): Promise<AlertNotificationRecord[]> {
  return apiFetch<AlertNotificationRecord[]>(`/alerts/history/${eventId}/notifications`);
}

// -- export (Phase 12) --------------------------------------------------

export type HistoryExportFormat = "json" | "ndjson";

/** Same filter shape as `getAlertHistory`, minus pagination (an export has
 * no page/page_size -- it always returns the whole matching set, capped
 * server-side per `format`). */
export type HistoryExportFilters = Omit<AlertHistoryFilters, "page" | "pageSize">;

function buildHistoryExportParams(
  filters: HistoryExportFilters,
  format: HistoryExportFormat,
): URLSearchParams {
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
  if (filters.includeTest) params.set("include_test", "true");
  params.set("format", format);
  return params;
}

/** Triggers a browser download of the history export (json or ndjson) --
 * see `downloadFile` for why this isn't a plain `apiFetch` call. */
export function downloadAlertHistoryExport(
  filters: HistoryExportFilters,
  format: HistoryExportFormat,
): Promise<void> {
  const params = buildHistoryExportParams(filters, format);
  const extension = format === "ndjson" ? "ndjson" : "json";
  return downloadFile(
    `/alerts/history/export?${params.toString()}`,
    `kam-alert-history.${extension}`,
  );
}
