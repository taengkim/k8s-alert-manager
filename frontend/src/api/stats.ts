import { apiFetch } from "./client";

export interface StatsFilters {
  teamId?: number;
  clusterIds?: number[];
  fromTs?: string;
  toTs?: string;
}

function buildParams(filters: StatsFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.teamId !== undefined) params.set("team_id", String(filters.teamId));
  for (const id of filters.clusterIds ?? []) params.append("cluster_id", String(id));
  if (filters.fromTs) params.set("from_ts", filters.fromTs);
  if (filters.toTs) params.set("to_ts", filters.toTs);
  return params;
}

export interface TopAlertRow {
  alertname: string;
  count: number;
  receive_total: number;
}

export function getTopAlerts(filters: StatsFilters, limit = 10): Promise<TopAlertRow[]> {
  const params = buildParams(filters);
  params.set("limit", String(limit));
  return apiFetch<TopAlertRow[]>(`/stats/top-alerts?${params.toString()}`);
}

export type VolumeBucket = "hour" | "day";

export interface VolumePoint {
  bucket_start: string;
  firing_count: number;
}

export function getVolume(filters: StatsFilters, bucket: VolumeBucket): Promise<VolumePoint[]> {
  const params = buildParams(filters);
  params.set("bucket", bucket);
  return apiFetch<VolumePoint[]>(`/stats/volume?${params.toString()}`);
}

export type BreakdownDimension = "namespace" | "severity" | "team" | "cluster";

export interface BreakdownRow {
  key: string;
  count: number;
}

export function getBreakdown(filters: StatsFilters, by: BreakdownDimension): Promise<BreakdownRow[]> {
  const params = buildParams(filters);
  params.set("by", by);
  return apiFetch<BreakdownRow[]>(`/stats/breakdown?${params.toString()}`);
}

export interface ResponseTimes {
  mtta_seconds: number | null;
  mttr_seconds: number | null;
  acked_count: number;
  resolved_count: number;
}

export function getResponseTimes(filters: StatsFilters): Promise<ResponseTimes> {
  const params = buildParams(filters);
  return apiFetch<ResponseTimes>(`/stats/response-times?${params.toString()}`);
}

export interface StatsSummary {
  firing_now: number;
  events_in_range: number;
  delivered_in_range: number;
  failed_or_dead_in_range: number;
}

export function getStatsSummary(filters: StatsFilters): Promise<StatsSummary> {
  const params = buildParams(filters);
  return apiFetch<StatsSummary>(`/stats/summary?${params.toString()}`);
}
