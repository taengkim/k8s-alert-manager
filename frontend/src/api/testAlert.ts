import { apiFetch } from "./client";
import type { RouteAction, RouteVerdict } from "./routes";

export interface TestAlertInput {
  cluster_id: number;
  alertname?: string;
  severity?: string;
  namespace?: string;
  labels?: Record<string, string>;
  annotations?: Record<string, string>;
}

export interface TestAlertVerdict {
  rule_id: number;
  rule_name: string;
  action: RouteAction;
  verdict: RouteVerdict;
}

export interface SuppressedBy {
  rule_id: number;
  rule_name: string;
}

export interface TestAlertResult {
  event_id: number;
  verdicts: TestAlertVerdict[];
  delivered_channels: string[];
  /** Set when a 'suppress' rule matched first -- route_event short-circuits
   * entirely in that case, so `verdicts` may still show a notify rule as
   * "matched" even though nothing was actually delivered. */
  suppressed_by: SuppressedBy | null;
}

export function fireTestAlert(teamId: number, body: TestAlertInput): Promise<TestAlertResult> {
  return apiFetch<TestAlertResult>(`/teams/${teamId}/test-alert`, { method: "POST", body });
}
