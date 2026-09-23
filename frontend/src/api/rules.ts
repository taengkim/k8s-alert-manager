import { apiFetch, downloadFile } from "./client";
import type { BuilderState } from "../components/rule-editor/builderExpr";

export type Severity = "critical" | "warning" | "info";
export type RuleMode = "builder" | "promql";

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
  /** "builder" only when the stored expr still matches its builder_state
   * annotation; a hand-edit after switching to PromQL mode falls back to
   * "promql" even if a stale annotation is still present. */
  mode: RuleMode;
  builder_state: BuilderState | null;
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
  mode: RuleMode;
  builder_state?: BuilderState;
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

// -- export/import (Phase 12) -----------------------------------------------

export type ConflictStrategy = "skip" | "overwrite" | "rename";

/** A portable rule (ownership metadata stripped) inside an export envelope. */
export interface ExportedRule {
  slug: string;
  alert_name: string;
  expr: string;
  for: string | null;
  severity: string;
  labels: Record<string, string>;
  annotations: Record<string, string>;
  runbook_url: string | null;
  grafana_url: string | null;
  mode: RuleMode;
  builder_state: BuilderState | null;
}

export interface RuleExportEnvelope {
  kam_export_version: number;
  kind: "rules";
  exported_at: string;
  source: { team_slug: string; cluster_name: string; app_version: string };
  rules: ExportedRule[];
}

/** Triggers a browser download of the export envelope -- see
 * `downloadFile` for why this isn't a plain `apiFetch` call. */
export function downloadRulesExport(
  teamId: number,
  clusterId: number,
  options: { slugs?: string[] } = {},
): Promise<void> {
  const params = new URLSearchParams({ cluster_id: String(clusterId) });
  if (options.slugs && options.slugs.length > 0) {
    params.set("slugs", options.slugs.join(","));
  }
  return downloadFile(
    `/teams/${teamId}/rules/export?${params.toString()}`,
    `kam-rules-team-${teamId}-cluster-${clusterId}.json`,
  );
}

export type ImportAction = "created" | "skipped" | "overwritten" | "renamed" | "failed";

export interface RuleImportVerdict {
  slug: string;
  action: ImportAction;
  final_slug: string | null;
  errors: string[];
  warnings: string[];
}

export interface RuleImportSummary {
  created: number;
  skipped: number;
  overwritten: number;
  renamed: number;
  failed: number;
}

export interface RuleImportResult {
  verdicts: RuleImportVerdict[];
  summary: RuleImportSummary;
}

export function importRules(
  teamId: number,
  body: {
    data: RuleExportEnvelope;
    targetClusterId: number;
    conflictStrategy: ConflictStrategy;
    dryRun: boolean;
  },
): Promise<RuleImportResult> {
  return apiFetch<RuleImportResult>(`/teams/${teamId}/rules/import`, {
    method: "POST",
    body: {
      data: body.data,
      target_cluster_id: body.targetClusterId,
      conflict_strategy: body.conflictStrategy,
      dry_run: body.dryRun,
    },
  });
}
