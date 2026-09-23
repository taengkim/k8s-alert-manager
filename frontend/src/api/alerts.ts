import { apiFetch } from "./client";

export type AlertState = "active" | "suppressed";

export interface LiveAlert {
  fingerprint: string;
  alertname: string;
  severity: string;
  namespace: string;
  cluster: string;
  state: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
  starts_at: string;
  generator_url: string | null;
  silenced_by: string[];
}

export interface AlertFetchError {
  cluster: string;
  message: string;
}

export interface LiveAlertsResponse {
  alerts: LiveAlert[];
  errors: AlertFetchError[];
}

export interface LiveAlertsFilters {
  teamId?: number;
  /** "none" matches alerts with no severity label. */
  severity?: string[];
  namespace?: string;
  state?: AlertState;
  search?: string;
}

export function getLiveAlerts(filters: LiveAlertsFilters): Promise<LiveAlertsResponse> {
  const params = new URLSearchParams();
  if (filters.teamId !== undefined) params.set("team_id", String(filters.teamId));
  if (filters.severity && filters.severity.length > 0) {
    params.set("severity", filters.severity.join(","));
  }
  if (filters.namespace) params.set("namespace", filters.namespace);
  if (filters.state) params.set("state", filters.state);
  if (filters.search) params.set("search", filters.search);

  const qs = params.toString();
  return apiFetch<LiveAlertsResponse>(`/alerts/live${qs ? `?${qs}` : ""}`);
}

export interface AckStatusItem {
  cluster: string;
  fingerprint: string;
}

export interface AckStatusMatch {
  cluster: string;
  fingerprint: string;
  event_id: number;
  acknowledged: boolean;
  assignee_username: string | null;
}

export function getAckStatus(
  teamId: number | undefined,
  items: AckStatusItem[],
): Promise<{ matched: AckStatusMatch[] }> {
  const qs = teamId !== undefined ? `?team_id=${teamId}` : "";
  return apiFetch<{ matched: AckStatusMatch[] }>(`/alerts/ack-status${qs}`, {
    method: "POST",
    body: { items },
  });
}
