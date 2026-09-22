export type TeamRole = "owner" | "member";
export type MembershipOrigin = "manual" | "ldap";

export interface TeamSummary {
  id: number;
  slug: string;
  name: string;
  role: TeamRole;
}

export interface CurrentUser {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  is_admin: boolean;
  teams: TeamSummary[];
}

export interface Team {
  id: number;
  slug: string;
  name: string;
  description: string | null;
}

export interface Member {
  membership_id: number;
  user_id: number;
  username: string;
  display_name: string;
  role: TeamRole;
  origin: MembershipOrigin;
}

export interface LdapMapping {
  id: number;
  ldap_group_dn: string;
  role: TeamRole;
}

export interface AdminUser {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  is_admin: boolean;
  is_active: boolean;
}

export interface Cluster {
  id: number;
  name: string;
  display_name: string;
  enabled: boolean;
}
