import { apiFetch } from "./client";
import type { LdapMapping, Member, Team, TeamRole } from "./types";

export function listTeams(): Promise<Team[]> {
  return apiFetch<Team[]>("/teams");
}

export function createTeam(body: { slug: string; name: string; description?: string }): Promise<Team> {
  return apiFetch<Team>("/teams", { method: "POST", body });
}

export function patchTeam(
  teamId: number,
  body: { name?: string; description?: string },
): Promise<Team> {
  return apiFetch<Team>(`/teams/${teamId}`, { method: "PATCH", body });
}

export function deleteTeam(teamId: number): Promise<void> {
  return apiFetch<void>(`/teams/${teamId}`, { method: "DELETE" });
}

export function listMembers(teamId: number): Promise<Member[]> {
  return apiFetch<Member[]>(`/teams/${teamId}/members`);
}

export function addMember(
  teamId: number,
  body: { user_id: number; role: TeamRole },
): Promise<Member> {
  return apiFetch<Member>(`/teams/${teamId}/members`, { method: "POST", body });
}

export function removeMember(teamId: number, membershipId: number): Promise<void> {
  return apiFetch<void>(`/teams/${teamId}/members/${membershipId}`, { method: "DELETE" });
}

export function listMappings(teamId: number): Promise<LdapMapping[]> {
  return apiFetch<LdapMapping[]>(`/teams/${teamId}/ldap-mappings`);
}

export function addMapping(
  teamId: number,
  body: { ldap_group_dn: string; role: TeamRole },
): Promise<LdapMapping> {
  return apiFetch<LdapMapping>(`/teams/${teamId}/ldap-mappings`, { method: "POST", body });
}

export function removeMapping(teamId: number, mappingId: number): Promise<void> {
  return apiFetch<void>(`/teams/${teamId}/ldap-mappings/${mappingId}`, { method: "DELETE" });
}
