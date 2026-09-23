import { apiFetch } from "./client";

export type SilenceStatus = "active" | "pending" | "expired";

export interface SilenceMatcher {
  name: string;
  value: string;
  isRegex: boolean;
}

export interface SilenceTeam {
  id: number;
  slug: string;
}

export interface SilenceCluster {
  id: number;
  name: string;
}

export interface SilenceOut {
  id: string;
  matchers: SilenceMatcher[];
  startsAt: string;
  endsAt: string;
  createdBy: string;
  comment: string;
  status: SilenceStatus;
  team: SilenceTeam | null;
  cluster: SilenceCluster;
}

export interface SilencesListResponse {
  silences: SilenceOut[];
}

export interface MatcherInput {
  name: string;
  value: string;
  is_regex: boolean;
}

export interface SilenceCreateInput {
  cluster_id: number;
  team_id: number;
  matchers: MatcherInput[];
  duration_minutes?: number;
  ends_at?: string;
  comment: string;
}

/** `clusterIds` omitted (or empty) defaults server-side to every enabled
 * cluster -- matches /alerts/live's fan-out default. */
export function listSilences(
  clusterIds?: number[],
  teamId?: number,
): Promise<SilencesListResponse> {
  const params = new URLSearchParams();
  for (const id of clusterIds ?? []) params.append("cluster_id", String(id));
  if (teamId !== undefined) params.set("team_id", String(teamId));
  const qs = params.toString();
  return apiFetch<SilencesListResponse>(`/silences${qs ? `?${qs}` : ""}`);
}

export function createSilence(body: SilenceCreateInput): Promise<SilenceOut> {
  return apiFetch<SilenceOut>("/silences", { method: "POST", body });
}

export function expireSilence(amSilenceId: string, clusterId: number): Promise<void> {
  return apiFetch<void>(
    `/silences/${encodeURIComponent(amSilenceId)}?cluster_id=${clusterId}`,
    { method: "DELETE" },
  );
}
