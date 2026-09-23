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

export interface SilenceOut {
  id: string;
  matchers: SilenceMatcher[];
  startsAt: string;
  endsAt: string;
  createdBy: string;
  comment: string;
  status: SilenceStatus;
  team: SilenceTeam | null;
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

export function listSilences(clusterId: number, teamId?: number): Promise<SilencesListResponse> {
  const params = new URLSearchParams({ cluster_id: String(clusterId) });
  if (teamId !== undefined) params.set("team_id", String(teamId));
  return apiFetch<SilencesListResponse>(`/silences?${params.toString()}`);
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
