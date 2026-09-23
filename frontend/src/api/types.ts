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

export interface ComponentHealth {
  ok: boolean;
  latency_ms: number | null;
  error?: string;
}

export interface ClusterHealth {
  k8s: ComponentHealth;
  prometheus: ComponentHealth;
  alertmanager: ComponentHealth;
}

export type K8sAuthKind = "incluster" | "kubeconfig" | "token";

export interface Cluster {
  id: number;
  name: string;
  display_name: string;
  enabled: boolean;
  /** A cache-only peek -- null until something (the admin page's own poll,
   * or a direct health fetch) has populated the health cache at least once. */
  health: ClusterHealth | null;
  heartbeat_state: string;
  last_heartbeat_at: string | null;
  // Admin-only fields -- present only when the caller is an admin.
  k8s_auth_kind?: K8sAuthKind;
  k8s_api_url?: string | null;
  prometheus_url?: string;
  alertmanager_url?: string;
  grafana_url?: string | null;
  rules_namespace?: string;
  heartbeat_enabled?: boolean;
  heartbeat_alertname?: string;
  heartbeat_timeout_seconds?: number;
  heartbeat_team_id?: number | null;
}

export type TokenAuthCredentials = { token: string; ca_cert?: string };

export interface ClusterWriteInput {
  name?: string; // create only -- immutable afterwards
  display_name: string;
  k8s_auth_kind: K8sAuthKind;
  k8s_api_url?: string;
  /** kubeconfig auth: raw kubeconfig YAML string. token auth: {token, ca_cert?}. */
  credentials?: string | TokenAuthCredentials | null;
  prometheus_url: string;
  alertmanager_url: string;
  grafana_url?: string | null;
  rules_namespace?: string;
  enabled?: boolean;
  heartbeat_enabled?: boolean;
  heartbeat_alertname?: string;
  heartbeat_timeout_seconds?: number;
  heartbeat_team_id?: number | null;
  rotate_webhook_token?: boolean;
}

/** Only present in the POST (create) / PATCH (rotate) response that just
 * (re)generated the webhook token -- never retrievable again afterwards. */
export interface ClusterSecretReveal {
  webhook_token: string;
  am_config_snippet: string;
}
