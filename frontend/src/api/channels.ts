import { apiFetch } from "./client";

/** A JSON Schema (draft 2020-12-ish) property description, as returned by
 * a channel type's `config_schema.model_json_schema()`. Only the subset
 * `JsonSchemaForm` actually renders is typed here -- see that file's
 * top-of-file note for the supported subset.
 */
export interface JsonSchemaProperty {
  type?: string | string[];
  format?: string;
  enum?: (string | number)[];
  items?: JsonSchemaProperty;
  default?: unknown;
  title?: string;
  description?: string;
  minItems?: number;
}

export interface JsonSchemaObject {
  type?: string;
  title?: string;
  properties: Record<string, JsonSchemaProperty>;
  required?: string[];
}

export interface ChannelType {
  type_name: string;
  display_name: string;
  json_schema: JsonSchemaObject;
}

export interface Channel {
  id: number;
  team_id: number;
  name: string;
  type: string;
  enabled: boolean;
  config: Record<string, unknown>;
  /** This channel's own default message template (Phase 13) -- null means
   * "use the channel type's default_templates, or the app default". */
  template_id: number | null;
  /** Phase 15: lets another team's routing rule pick this channel as an
   * escalation target (see listEscalationTargets). */
  allow_cross_team_escalation: boolean;
}

export interface ChannelCreateInput {
  name: string;
  type: string;
  config: Record<string, unknown>;
  template_id?: number | null;
  allow_cross_team_escalation?: boolean;
}

export interface ChannelUpdateInput {
  name?: string;
  config?: Record<string, unknown>;
  enabled?: boolean;
  /** Included in the request only when actually changed -- see
   * Channels.tsx's update mutation. `null` explicitly clears it. */
  template_id?: number | null;
  allow_cross_team_escalation?: boolean;
}

/** One selectable escalation target (Phase 15): a team's own channels plus
 * any other team's channel with allow_cross_team_escalation=true. Powers
 * RouteEditor's escalation channel Select. */
export interface EscalationTarget {
  id: number;
  name: string;
  team_slug: string;
}

export function listChannelTypes(): Promise<ChannelType[]> {
  return apiFetch<ChannelType[]>("/channel-types");
}

export function listChannels(teamId: number): Promise<Channel[]> {
  return apiFetch<Channel[]>(`/teams/${teamId}/channels`);
}

export function createChannel(teamId: number, body: ChannelCreateInput): Promise<Channel> {
  return apiFetch<Channel>(`/teams/${teamId}/channels`, { method: "POST", body });
}

export function patchChannel(channelId: number, body: ChannelUpdateInput): Promise<Channel> {
  return apiFetch<Channel>(`/channels/${channelId}`, { method: "PATCH", body });
}

export function deleteChannel(channelId: number): Promise<void> {
  return apiFetch<void>(`/channels/${channelId}`, { method: "DELETE" });
}

export function testChannel(channelId: number): Promise<{ status: string }> {
  return apiFetch<{ status: string }>(`/channels/${channelId}/test`, { method: "POST" });
}

export function listEscalationTargets(teamId: number): Promise<EscalationTarget[]> {
  return apiFetch<EscalationTarget[]>(`/channels/escalation-targets?team_id=${teamId}`);
}
