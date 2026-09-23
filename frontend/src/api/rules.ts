import { apiFetch } from "./client";

export type Severity = "critical" | "warning" | "info";

export interface RuleOut {
  slug: string;
  alert_name: string;
  expr: string;
  for: string | null;
  severity: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
  runbook_url: string | null;
  grafana_url: string | null;
  /** Prometheus rule health: "ok" | "err" | "unknown" (unknown while Prometheus is unreachable). */
  health: string;
  state: string | null;
  last_error: string | null;
}

export interface RulesListResponse {
  rules: RuleOut[];
  /** Present when Prometheus was unreachable -- health on every rule degrades to "unknown". */
  warning?: string;
}

export interface RuleWriteInput {
  slug: string;
  alert_name: string;
  expr: string;
  for?: string;
  severity: Severity;
  labels?: Record<string, string>;
  annotations?: Record<string, string>;
  runbook_url?: string;
  grafana_url?: string;
}

export interface ValidateResult {
  valid: boolean;
  error: string | null;
}

export function listRules(teamId: number, clusterId: number): Promise<RulesListResponse> {
  return apiFetch<RulesListResponse>(`/teams/${teamId}/rules?cluster_id=${clusterId}`);
}

export function getRule(teamId: number, clusterId: number, slug: string): Promise<RuleOut> {
  return apiFetch<RuleOut>(`/teams/${teamId}/rules/${slug}?cluster_id=${clusterId}`);
}

export function createRule(
  teamId: number,
  clusterId: number,
  body: RuleWriteInput,
): Promise<RuleOut> {
  return apiFetch<RuleOut>(`/teams/${teamId}/rules?cluster_id=${clusterId}`, {
    method: "POST",
    body,
  });
}

export function updateRule(
  teamId: number,
  clusterId: number,
  slug: string,
  body: RuleWriteInput,
): Promise<RuleOut> {
  return apiFetch<RuleOut>(`/teams/${teamId}/rules/${slug}?cluster_id=${clusterId}`, {
    method: "PUT",
    body,
  });
}

export function deleteRule(teamId: number, clusterId: number, slug: string): Promise<void> {
  return apiFetch<void>(`/teams/${teamId}/rules/${slug}?cluster_id=${clusterId}`, {
    method: "DELETE",
  });
}

export function validateExpr(clusterId: number, expr: string): Promise<ValidateResult> {
  return apiFetch<ValidateResult>("/rules/validate", {
    method: "POST",
    body: { cluster_id: clusterId, expr },
  });
}
