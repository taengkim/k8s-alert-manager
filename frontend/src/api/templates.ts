import { apiFetch } from "./client";

export interface MessageTemplate {
  id: number;
  team_id: number;
  name: string;
  description: string | null;
  kind: string;
  title_template: string;
  body_template: string;
  body_html_template: string | null;
  created_at: string;
  updated_at: string;
  /** How many (non-deleted) channels/routing rules currently point at this
   * template -- shown on the list page, and what a delete's response
   * summarizes as reverted to their next-priority default. */
  channel_count: number;
  route_count: number;
}

export interface TemplateWriteInput {
  name: string;
  description?: string;
  kind?: string;
  title_template: string;
  body_template: string;
  body_html_template?: string | null;
}

export interface TemplateDeleteResult {
  deleted: boolean;
  unassigned_channels: number;
  unassigned_routes: number;
  detail: string;
}

export interface TemplateVariable {
  name: string;
  description: string;
  example: string;
}

export interface RenderedPreview {
  title: string;
  body: string;
  body_html: string | null;
}

export interface TemplatePreviewError {
  slot: "title" | "body" | "body_html" | null;
  lineno: number | null;
  message: string;
}

export interface TemplatePreviewResult {
  rendered: RenderedPreview | null;
  errors: TemplatePreviewError[];
  warnings: string[];
}

export interface TemplatePreviewInput {
  title_template: string;
  body_template: string;
  body_html_template?: string | null;
  alert_event_id?: number;
  use_sample?: boolean;
}

export function listTemplates(teamId: number): Promise<MessageTemplate[]> {
  return apiFetch<MessageTemplate[]>(`/teams/${teamId}/templates`);
}

export function createTemplate(
  teamId: number,
  body: TemplateWriteInput,
): Promise<MessageTemplate> {
  return apiFetch<MessageTemplate>(`/teams/${teamId}/templates`, { method: "POST", body });
}

export function getTemplate(templateId: number): Promise<MessageTemplate> {
  return apiFetch<MessageTemplate>(`/templates/${templateId}`);
}

export function updateTemplate(
  templateId: number,
  body: TemplateWriteInput,
): Promise<MessageTemplate> {
  return apiFetch<MessageTemplate>(`/templates/${templateId}`, { method: "PUT", body });
}

export function deleteTemplate(templateId: number): Promise<TemplateDeleteResult> {
  return apiFetch<TemplateDeleteResult>(`/templates/${templateId}`, { method: "DELETE" });
}

export function previewTemplate(body: TemplatePreviewInput): Promise<TemplatePreviewResult> {
  return apiFetch<TemplatePreviewResult>("/templates/preview", { method: "POST", body });
}

export function listTemplateVariables(kind: string = "alert"): Promise<TemplateVariable[]> {
  return apiFetch<TemplateVariable[]>(`/templates/variables?kind=${encodeURIComponent(kind)}`);
}
