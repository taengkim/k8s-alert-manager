import { apiFetch } from "./client";
import type { MatcherKind, MatcherTarget } from "./routes";

export type ShareMode = "view" | "view_notify";

export interface ShareMatcher {
  kind: MatcherKind;
  target: MatcherTarget;
  key?: string | null;
  pattern: string;
}

/** A team's (id, slug, name) only -- see GET /teams/all-brief's docstring
 * for why this exists alongside the member-scoped GET /teams. */
export interface TeamBrief {
  id: number;
  slug: string;
  name: string;
}

export interface OutgoingShare {
  id: number;
  owner_team_id: number;
  target_team_id: number;
  target_team_slug: string;
  target_team_name: string;
  mode: ShareMode;
  matchers: ShareMatcher[] | null;
  created_at: string;
}

export interface IncomingShare {
  id: number;
  owner_team_id: number;
  owner_team_slug: string;
  owner_team_name: string;
  mode: ShareMode;
  matchers: ShareMatcher[] | null;
  created_at: string;
}

export interface ShareCreateInput {
  target_team_id: number;
  mode: ShareMode;
  matchers?: ShareMatcher[];
}

/** All fields optional -- only keys actually present are applied server-side
 * (a partial update via PUT, not a full-replace like RouteWriteInput). Pass
 * `matchers: null` to explicitly clear a share back to "every alert". */
export interface ShareUpdateInput {
  mode?: ShareMode;
  matchers?: ShareMatcher[] | null;
}

export function listAllTeamsBrief(): Promise<TeamBrief[]> {
  return apiFetch<TeamBrief[]>("/teams/all-brief");
}

export function listOutgoingShares(teamId: number): Promise<OutgoingShare[]> {
  return apiFetch<OutgoingShare[]>(`/teams/${teamId}/shares`);
}

export function createShare(teamId: number, body: ShareCreateInput): Promise<OutgoingShare> {
  return apiFetch<OutgoingShare>(`/teams/${teamId}/shares`, { method: "POST", body });
}

export function updateShare(shareId: number, body: ShareUpdateInput): Promise<OutgoingShare> {
  return apiFetch<OutgoingShare>(`/shares/${shareId}`, { method: "PUT", body });
}

export function deleteShare(shareId: number): Promise<void> {
  return apiFetch<void>(`/shares/${shareId}`, { method: "DELETE" });
}

export function listIncomingShares(teamId: number): Promise<IncomingShare[]> {
  return apiFetch<IncomingShare[]>(`/teams/${teamId}/shared-with-me`);
}
