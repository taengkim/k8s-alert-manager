import { apiFetch } from "./client";

export type RouteAction = "notify" | "suppress";
export type MatcherKind = "include" | "exclude";
export type MatcherTarget = "alertname" | "label" | "annotation";

export interface RouteMatcher {
  kind: MatcherKind;
  target: MatcherTarget;
  key?: string | null;
  pattern: string;
  /** Present on rows read back from the API; ignored on write (position is
   * assigned server-side from array order). */
  position?: number;
}

export interface RouteOut {
  id: number;
  team_id: number;
  name: string;
  description: string | null;
  action: RouteAction;
  enabled: boolean;
  notify_on_firing: boolean;
  notify_on_resolved: boolean;
  /** Phase 14: when true, this rule also evaluates against alerts shared
   * into this team via a 'view_notify' AlertShare. */
  include_shared: boolean;
  severities: string[] | null;
  namespaces_include: string[] | null;
  namespaces_exclude: string[] | null;
  clusters: number[] | null;
  template_id: number | null;
  channel_ids: number[];
  /** Phase 15: escalation -- schedules a follow-up notification through
   * escalation_channel_ids if the event this rule matched is still firing
   * and unacknowledged escalation_after_minutes later. */
  escalation_enabled: boolean;
  escalation_after_minutes: number | null;
  escalation_channel_ids: number[];
  /** Phase 15: re-delivers to this rule's own channel_ids every N minutes
   * while the event stays firing and unacknowledged. null disables it. */
  renotify_interval_minutes: number | null;
  matchers: RouteMatcher[];
  created_at: string;
  updated_at: string;
}

export interface RouteWriteInput {
  name: string;
  description?: string;
  action: RouteAction;
  enabled: boolean;
  notify_on_firing: boolean;
  notify_on_resolved: boolean;
  include_shared: boolean;
  severities?: string[];
  namespaces_include?: string[];
  namespaces_exclude?: string[];
  clusters?: number[];
  /** This rule's own message template (Phase 13) -- takes priority over
   * the channel's template_id. `undefined`/omitted means none. */
  template_id?: number | null;
  channel_ids: number[];
  escalation_enabled: boolean;
  escalation_after_minutes?: number;
  escalation_channel_ids: number[];
  renotify_interval_minutes?: number;
  matchers: RouteMatcher[];
}

export type RouteVerdict =
  | "matched"
  | "cluster_filtered"
  | "gated"
  | "severity_filtered"
  | "namespace_filtered"
  | "not_included"
  | "excluded";

export interface RoutePreviewItem {
  event_id: number;
  alertname: string;
  severity: string | null;
  namespace: string | null;
  cluster: string;
  /** The event's actual stored status ("firing" | "resolved") -- the
   * verdict itself is always computed as if the alert had just fired
   * (trigger="firing"), regardless of this. */
  status: string;
  verdict: RouteVerdict;
  blocking_matcher_position: number | null;
}

export function listRoutes(teamId: number): Promise<RouteOut[]> {
  return apiFetch<RouteOut[]>(`/teams/${teamId}/routes`);
}

export function createRoute(teamId: number, body: RouteWriteInput): Promise<RouteOut> {
  return apiFetch<RouteOut>(`/teams/${teamId}/routes`, { method: "POST", body });
}

export function getRoute(routeId: number): Promise<RouteOut> {
  return apiFetch<RouteOut>(`/routes/${routeId}`);
}

export function updateRoute(routeId: number, body: RouteWriteInput): Promise<RouteOut> {
  return apiFetch<RouteOut>(`/routes/${routeId}`, { method: "PUT", body });
}

export function deleteRoute(routeId: number): Promise<void> {
  return apiFetch<void>(`/routes/${routeId}`, { method: "DELETE" });
}

export function previewRoute(teamId: number, body: RouteWriteInput): Promise<RoutePreviewItem[]> {
  return apiFetch<RoutePreviewItem[]>(`/teams/${teamId}/routes/preview`, {
    method: "POST",
    body,
  });
}

export function listNamespaces(clusterId: number): Promise<string[]> {
  return apiFetch<string[]>(`/namespaces?cluster_id=${clusterId}`);
}
