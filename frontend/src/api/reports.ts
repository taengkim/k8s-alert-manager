import { apiFetch } from "./client";

export type ReportCadence = "daily" | "weekly" | "monthly";

export interface ReportSchedule {
  id: number;
  team_id: number;
  name: string;
  enabled: boolean;
  cadence: ReportCadence;
  /** 0=Monday .. 6=Sunday. Only set (and only meaningful) for cadence='weekly'. */
  weekday: number | null;
  hour: number;
  /** IANA zone name (e.g. "Asia/Seoul"). */
  timezone: string;
  /** A kind='report' MessageTemplate id, or null to use the built-in default. */
  template_id: number | null;
  channel_ids: number[];
  next_run_at: string;
  last_run_at: string | null;
  /** 'ok' | 'error: ...' | null (never run yet). */
  last_status: string | null;
  created_at: string;
}

export interface ReportScheduleWriteInput {
  name: string;
  enabled?: boolean;
  cadence: ReportCadence;
  weekday?: number | null;
  hour: number;
  timezone?: string;
  template_id?: number | null;
  channel_ids: number[];
}

export interface ReportSchedulePreview {
  title: string;
  body: string;
  body_html: string | null;
}

export function listReportSchedules(teamId: number): Promise<ReportSchedule[]> {
  return apiFetch<ReportSchedule[]>(`/teams/${teamId}/reports`);
}

export function createReportSchedule(
  teamId: number,
  body: ReportScheduleWriteInput,
): Promise<ReportSchedule> {
  return apiFetch<ReportSchedule>(`/teams/${teamId}/reports`, { method: "POST", body });
}

export function updateReportSchedule(
  reportId: number,
  body: Partial<ReportScheduleWriteInput>,
): Promise<ReportSchedule> {
  return apiFetch<ReportSchedule>(`/reports/${reportId}`, { method: "PATCH", body });
}

export function deleteReportSchedule(reportId: number): Promise<void> {
  return apiFetch<void>(`/reports/${reportId}`, { method: "DELETE" });
}

export function runReportNow(reportId: number): Promise<{ queued_channels: number }> {
  return apiFetch<{ queued_channels: number }>(`/reports/${reportId}/run-now`, { method: "POST" });
}

export function previewReportSchedule(reportId: number): Promise<ReportSchedulePreview> {
  return apiFetch<ReportSchedulePreview>(`/reports/${reportId}/preview`);
}
