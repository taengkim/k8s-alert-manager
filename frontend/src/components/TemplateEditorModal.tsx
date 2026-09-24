import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Form, Input, List, Modal, Radio, Select, Space, Tag, Typography } from "antd";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import { getAlertHistory } from "../api/history";
import {
  createTemplate,
  getTemplate,
  listTemplateVariables,
  previewTemplate,
  updateTemplate,
} from "../api/templates";
import type { TemplatePreviewResult, TemplateVariable, TemplateWriteInput } from "../api/templates";
import { useI18n } from "../i18n";

const { Text, Title, Paragraph } = Typography;

const TITLE_FIELD_ID = "template-editor-title";
const BODY_FIELD_ID = "template-editor-body";
const BODY_HTML_FIELD_ID = "template-editor-body-html";

const PREVIEW_DEBOUNCE_MS = 500;
const SAMPLE_SOURCE = "sample";

type TemplateKind = "alert" | "report";

interface FormValues {
  name: string;
  description?: string;
  kind: TemplateKind;
  title_template: string;
  body_template: string;
  body_html_template?: string;
}

interface TemplateEditorModalProps {
  open: boolean;
  onClose: () => void;
  /** null/undefined = create mode. */
  templateId?: number | null;
}

function toBody(values: FormValues): TemplateWriteInput {
  return {
    name: values.name,
    description: values.description || undefined,
    kind: values.kind,
    title_template: values.title_template,
    body_template: values.body_template,
    body_html_template: values.body_html_template || undefined,
  };
}

/** Inserts `text` at the given field's current cursor position (falling
 * back to appending at the end if the DOM node can't be found -- e.g. the
 * field hasn't mounted yet), then restores focus and cursor placement right
 * after the inserted text.
 */
function insertAtCursor(
  elementId: string,
  fieldName: keyof FormValues,
  form: ReturnType<typeof Form.useForm<FormValues>>[0],
  text: string,
): void {
  const el = document.getElementById(elementId) as
    | HTMLInputElement
    | HTMLTextAreaElement
    | null;
  const current = (form.getFieldValue(fieldName) as string | undefined) ?? "";

  if (!el) {
    form.setFieldValue(fieldName, current + text);
    return;
  }

  const start = el.selectionStart ?? current.length;
  const end = el.selectionEnd ?? current.length;
  const next = current.slice(0, start) + text + current.slice(end);
  form.setFieldValue(fieldName, next);

  const cursor = start + text.length;
  requestAnimationFrame(() => {
    el.focus();
    el.setSelectionRange(cursor, cursor);
  });
}

export default function TemplateEditorModal({ open, onClose, templateId }: TemplateEditorModalProps) {
  const { t } = useI18n();
  const isEdit = templateId != null;
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnClose
      width={960}
      style={{ top: 24 }}
      styles={{ body: { maxHeight: "calc(100vh - 160px)", overflowY: "auto" } }}
      title={isEdit ? t("templateEditor.editTitle") : t("templateEditor.createTitle")}
    >
      {/* Gated on `open` so this form is a fresh component instance every
          time it's opened -- reopening for a different template (or for
          create, right after editing one) always starts clean. */}
      {open && <TemplateEditorFormBody templateId={templateId ?? null} onClose={onClose} />}
    </Modal>
  );
}

function TemplateEditorFormBody({
  templateId,
  onClose,
}: {
  templateId: number | null;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const isEdit = templateId != null;
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { currentTeam } = useTeam();
  const [form] = Form.useForm<FormValues>();
  const [serverError, setServerError] = useState<string | null>(null);
  const [activeField, setActiveField] = useState<{
    elementId: string;
    fieldName: keyof FormValues;
  }>({ elementId: BODY_FIELD_ID, fieldName: "body_template" });

  const [previewSource, setPreviewSource] = useState<string>(SAMPLE_SOURCE);
  const [previewResult, setPreviewResult] = useState<TemplatePreviewResult | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const teamId = currentTeam?.id;
  const titleValue = Form.useWatch("title_template", form);
  const bodyValue = Form.useWatch("body_template", form);
  const bodyHtmlValue = Form.useWatch("body_html_template", form);
  const kindValue: TemplateKind = Form.useWatch("kind", form) ?? "alert";
  const isReportKind = kindValue === "report";

  const templateQuery = useQuery({
    queryKey: ["template", templateId],
    queryFn: () => getTemplate(templateId!),
    enabled: isEdit,
  });

  const variablesQuery = useQuery({
    queryKey: ["template-variables", kindValue],
    queryFn: () => listTemplateVariables(kindValue),
  });

  // Only relevant for kind='alert' -- a report template previews against
  // real ReportData via a schedule's own GET /reports/{id}/preview (see the
  // Reports tab), not a sample alert event.
  const recentEventsQuery = useQuery({
    queryKey: ["template-preview-events", teamId],
    queryFn: () => getAlertHistory({ teamId, includeTest: true, pageSize: 20 }),
    enabled: !!teamId && !isReportKind,
  });

  useEffect(() => {
    if (!templateQuery.data) return;
    const t = templateQuery.data;
    form.setFieldsValue({
      name: t.name,
      description: t.description ?? undefined,
      kind: (t.kind as TemplateKind) ?? "alert",
      title_template: t.title_template,
      body_template: t.body_template,
      body_html_template: t.body_html_template ?? undefined,
    });
  }, [templateQuery.data, form]);

  // Live preview: 500ms debounce after the last edit to any slot or source
  // change, then POST /templates/preview. A ref (not a bare closure captured
  // by setTimeout) guards against a slower, earlier request's response
  // clobbering a faster, later one if they resolve out of order.
  const latestRequestId = useRef(0);
  useEffect(() => {
    const requestId = ++latestRequestId.current;
    const handle = setTimeout(() => {
      void (async () => {
        if (isReportKind) {
          // No sample AlertNotification makes sense for a report template
          // -- preview it via a real schedule's own GET /reports/{id}/preview
          // (see the Reports tab) instead.
          setPreviewResult(null);
          setPreviewError(null);
          return;
        }
        if (!titleValue && !bodyValue) {
          setPreviewResult(null);
          setPreviewError(null);
          return;
        }
        setPreviewLoading(true);
        setPreviewError(null);
        try {
          const result = await previewTemplate({
            title_template: titleValue ?? "",
            body_template: bodyValue ?? "",
            body_html_template: bodyHtmlValue || undefined,
            ...(previewSource === SAMPLE_SOURCE
              ? { use_sample: true }
              : { alert_event_id: Number(previewSource) }),
          });
          if (requestId === latestRequestId.current) {
            setPreviewResult(result);
          }
        } catch (err) {
          if (requestId === latestRequestId.current) {
            setPreviewError(err instanceof ApiError ? err.detail : t("preview.loadError"));
            setPreviewResult(null);
          }
        } finally {
          if (requestId === latestRequestId.current) {
            setPreviewLoading(false);
          }
        }
      })();
    }, PREVIEW_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [titleValue, bodyValue, bodyHtmlValue, previewSource, isReportKind]);

  const saveMutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = toBody(values);
      return isEdit ? updateTemplate(templateId!, body) : createTemplate(teamId!, body);
    },
    onSuccess: () => {
      message.success(isEdit ? t("templateEditor.updateSuccess") : t("templateEditor.createSuccess"));
      queryClient.invalidateQueries({ queryKey: ["templates", teamId] });
      onClose();
    },
    onError: (err) => {
      setServerError(
        err instanceof ApiError
          ? err.detail
          : isEdit
            ? t("templateEditor.updateError")
            : t("templateEditor.createError"),
      );
    },
  });

  const handleFinish = (values: FormValues) => {
    setServerError(null);
    saveMutation.mutate(values);
  };

  const handleInsertVariable = (variable: TemplateVariable) => {
    // `variable.example` is a ready-to-use Jinja snippet the backend already
    // computes per variable (e.g. "{{ labels.pod }}", "{{ now() | datetime_format }}")
    // -- inserting `{{ ${variable.name} }}` directly would produce a literal
    // syntax error for placeholder-style names like "labels.<key>" (the
    // "<key>" segment isn't a valid identifier), rejected at save time.
    insertAtCursor(activeField.elementId, activeField.fieldName, form, variable.example);
  };

  if (!currentTeam) {
    return <Alert type="info" showIcon message={t("common.noTeamAssigned")} />;
  }

  const errorsBySlot = new Map<string, { lineno: number | null; message: string }[]>();
  for (const error of previewResult?.errors ?? []) {
    const key = error.slot ?? "_general";
    const list = errorsBySlot.get(key) ?? [];
    list.push({ lineno: error.lineno, message: error.message });
    errorsBySlot.set(key, list);
  }

  const eventOptions = (recentEventsQuery.data?.items ?? []).map((item) => ({
    value: String(item.id),
    label: `#${item.id} · ${item.alertname} · ${item.status}`,
  }));

  return (
    <div style={{ display: "flex", gap: 24, alignItems: "flex-start" }}>
      <div style={{ flex: "1 1 640px", minWidth: 480 }}>
        {serverError && (
          <Alert type="error" showIcon message={serverError} style={{ marginBottom: 16 }} />
        )}

        <Form<FormValues>
          form={form}
          layout="vertical"
          onFinish={handleFinish}
          initialValues={{ kind: "alert" }}
        >
          <Form.Item
            name="name"
            label={t("common.name")}
            rules={[{ required: true, message: t("common.nameRequired") }]}
          >
            <Input placeholder="critical-alert-template" />
          </Form.Item>
          <Form.Item name="description" label={t("common.description")}>
            <Input placeholder={t("templateEditor.descriptionPlaceholder")} />
          </Form.Item>
          <Form.Item
            name="kind"
            label={t("templates.kindColumn")}
            help={isEdit ? t("templateEditor.kindLockedHelp") : t("templateEditor.reportKindHelp")}
          >
            <Radio.Group
              disabled={isEdit}
              options={[
                { label: t("templates.kindAlert"), value: "alert" },
                { label: t("templates.kindReport"), value: "report" },
              ]}
              optionType="button"
            />
          </Form.Item>

          <Form.Item
            name="title_template"
            label={t("templateEditor.titleFieldLabel")}
            rules={[{ required: true, message: t("templateEditor.titleRequired") }]}
            help={errorsBySlot.get("title")?.map((e, i) => (
              <Text type="danger" key={i} style={{ display: "block" }}>
                {e.lineno != null ? t("templateEditor.lineNumberPrefix", { line: e.lineno }) : ""}
                {e.message}
              </Text>
            ))}
          >
            <Input
              id={TITLE_FIELD_ID}
              style={{ fontFamily: "monospace" }}
              placeholder="[{{ severity | upper }}] {{ alertname }}"
              onFocus={() => setActiveField({ elementId: TITLE_FIELD_ID, fieldName: "title_template" })}
            />
          </Form.Item>

          <Form.Item
            name="body_template"
            label={t("templateEditor.bodyFieldLabel")}
            rules={[{ required: true, message: t("templateEditor.bodyRequired") }]}
            help={errorsBySlot.get("body")?.map((e, i) => (
              <Text type="danger" key={i} style={{ display: "block" }}>
                {e.lineno != null ? t("templateEditor.lineNumberPrefix", { line: e.lineno }) : ""}
                {e.message}
              </Text>
            ))}
          >
            <Input.TextArea
              id={BODY_FIELD_ID}
              rows={8}
              style={{ fontFamily: "monospace" }}
              onFocus={() => setActiveField({ elementId: BODY_FIELD_ID, fieldName: "body_template" })}
            />
          </Form.Item>

          <Form.Item
            name="body_html_template"
            label={t("templateEditor.bodyHtmlFieldLabel")}
            help={errorsBySlot.get("body_html")?.map((e, i) => (
              <Text type="danger" key={i} style={{ display: "block" }}>
                {e.lineno != null ? t("templateEditor.lineNumberPrefix", { line: e.lineno }) : ""}
                {e.message}
              </Text>
            ))}
          >
            <Input.TextArea
              id={BODY_HTML_FIELD_ID}
              rows={8}
              style={{ fontFamily: "monospace" }}
              placeholder={t("templateEditor.bodyHtmlPlaceholder")}
              onFocus={() =>
                setActiveField({ elementId: BODY_HTML_FIELD_ID, fieldName: "body_html_template" })
              }
            />
          </Form.Item>

          {errorsBySlot.get("_general")?.map((e, i) => (
            <Alert key={i} type="error" showIcon message={e.message} style={{ marginBottom: 16 }} />
          ))}

          <Space>
            <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>
              {t("common.save")}
            </Button>
            <Button onClick={onClose}>{t("common.cancel")}</Button>
          </Space>
        </Form>
      </div>

      <div style={{ flex: "0 0 360px", minWidth: 320 }}>
        <Title level={5}>{t("templateEditor.variableRefTitle")}</Title>
        <Paragraph type="secondary" style={{ marginTop: -8 }}>
          {t("templateEditor.variableRefHint")}
        </Paragraph>
        <List
          size="small"
          bordered
          loading={variablesQuery.isLoading}
          dataSource={variablesQuery.data ?? []}
          style={{ marginBottom: 24, maxHeight: 320, overflowY: "auto" }}
          renderItem={(variable) => (
            <List.Item
              style={{ cursor: "pointer" }}
              onClick={() => handleInsertVariable(variable)}
            >
              <div>
                <Text code>{variable.name}</Text>
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {variable.description}
                  </Text>
                </div>
              </div>
            </List.Item>
          )}
        />

        <Title level={5}>{t("templateEditor.livePreviewTitle")}</Title>
        {isReportKind ? (
          <Alert
            type="info"
            showIcon
            message={t("templateEditor.reportPreviewUnavailable")}
            description={t("templateEditor.reportPreviewUnavailableDesc")}
          />
        ) : (
          <>
            <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }}>
              <Select
                style={{ width: "100%" }}
                value={previewSource}
                onChange={setPreviewSource}
                options={[{ value: SAMPLE_SOURCE, label: t("templateEditor.sampleAlertOption") }, ...eventOptions]}
                loading={recentEventsQuery.isLoading}
              />
            </Space>

            {previewError && (
              <Alert type="error" showIcon message={previewError} style={{ marginBottom: 12 }} />
            )}

            {previewResult?.warnings && previewResult.warnings.length > 0 && (
              <Alert
                type="warning"
                showIcon
                message={t("templateEditor.undefinedVariablesTitle")}
                description={
                  <Space size={4} wrap>
                    {previewResult.warnings.map((w) => (
                      <Tag key={w} color="gold">
                        {w}
                      </Tag>
                    ))}
                  </Space>
                }
                style={{ marginBottom: 12 }}
              />
            )}

            <div
              style={{
                border: "1px solid #d9d9d9",
                borderRadius: 6,
                padding: 12,
                minHeight: 200,
                opacity: previewLoading ? 0.6 : 1,
              }}
            >
              {previewResult?.rendered ? (
                <>
                  <div style={{ marginBottom: 8 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {t("templateEditor.renderedTitleLabel")}
                    </Text>
                    <div style={{ fontWeight: 600 }}>{previewResult.rendered.title}</div>
                  </div>
                  <div style={{ marginBottom: previewResult.rendered.body_html ? 8 : 0 }}>
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {t("templateEditor.renderedBodyLabel")}
                    </Text>
                    <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontFamily: "monospace" }}>
                      {previewResult.rendered.body}
                    </pre>
                  </div>
                  {previewResult.rendered.body_html && (
                    <div>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {t("templateEditor.renderedBodyHtmlLabel")}
                      </Text>
                      {/* Deliberately rendered as plain escaped text (React's
                          {} interpolation, not dangerouslySetInnerHTML) -- this
                          panel shows the rendered HTML *source* for inspection,
                          never executes it. A visual HTML preview is out of
                          scope for this phase. */}
                      <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontFamily: "monospace" }}>
                        {previewResult.rendered.body_html}
                      </pre>
                    </div>
                  )}
                </>
              ) : (
                <Text type="secondary">
                  {previewResult && previewResult.errors.length > 0
                    ? t("templateEditor.previewBlockedByErrors")
                    : t("templateEditor.previewPromptEmpty")}
                </Text>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
