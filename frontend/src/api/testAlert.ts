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

export interface TestAlertResult {
  event_id: number;
  verdicts: TestAlertVerdict[];
  delivered_channels: string[];
}

export function fireTestAlert(teamId: number, body: TestAlertInput): Promise<TestAlertResult> {
  return apiFetch<TestAlertResult>(`/teams/${teamId}/test-alert`, { method: "POST", body });
}
