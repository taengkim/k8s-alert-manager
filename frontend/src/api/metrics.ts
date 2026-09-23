import { apiFetch } from "./client";

export interface MetricNamesResponse {
  names: string[];
}

export interface MetricMetadata {
  type?: string;
  help?: string;
  unit?: string;
}

export interface MetricLabelsResponse {
  labels: string[];
}

export interface MetricLabelValuesResponse {
  values: string[];
}

export interface InstantQuerySample {
  labels: Record<string, string>;
  value: number | null;
}

export interface InstantQueryResult {
  result_type: string;
  series_count: number;
  samples: InstantQuerySample[];
}

export interface RangeSeries {
  labels: Record<string, string>;
  points: [number, number | null][];
}

export interface QueryRangeResult {
  series: RangeSeries[];
  truncated: boolean;
  total_series: number;
  step_used: number;
}

export function fetchMetricNames(
  clusterId: number,
  search?: string,
  limit?: number,
): Promise<MetricNamesResponse> {
  const params = new URLSearchParams({ cluster_id: String(clusterId) });
  if (search) params.set("search", search);
  if (limit) params.set("limit", String(limit));
  return apiFetch<MetricNamesResponse>(`/metrics/names?${params}`);
}

export function fetchMetricMetadata(
  clusterId: number,
  metric: string,
): Promise<MetricMetadata> {
  const params = new URLSearchParams({ cluster_id: String(clusterId), metric });
  return apiFetch<MetricMetadata>(`/metrics/metadata?${params}`);
}

export function fetchMetricLabels(
  clusterId: number,
  metric: string,
): Promise<MetricLabelsResponse> {
  const params = new URLSearchParams({ cluster_id: String(clusterId), metric });
  return apiFetch<MetricLabelsResponse>(`/metrics/labels?${params}`);
}

export function fetchLabelValues(
  clusterId: number,
  metric: string,
  label: string,
): Promise<MetricLabelValuesResponse> {
  const params = new URLSearchParams({ cluster_id: String(clusterId), metric, label });
  return apiFetch<MetricLabelValuesResponse>(`/metrics/label-values?${params}`);
}

export function runInstantQuery(clusterId: number, query: string): Promise<InstantQueryResult> {
  return apiFetch<InstantQueryResult>("/metrics/query", {
    method: "POST",
    body: { cluster_id: clusterId, query },
  });
}

export function runQueryRange(
  clusterId: number,
  query: string,
  start: number,
  end: number,
  step?: number,
): Promise<QueryRangeResult> {
  return apiFetch<QueryRangeResult>("/metrics/query_range", {
    method: "POST",
    body: { cluster_id: clusterId, query, start, end, step },
  });
}
