import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router";
import { Alert, App, Button, Form, Input, List, Select, Space, Tag, Typography } from "antd";
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

const { Text, Title, Paragraph } = Typography;

const TITLE_FIELD_ID = "template-editor-title";
const BODY_FIELD_ID = "template-editor-body";
const BODY_HTML_FIELD_ID = "template-editor-body-html";

const PREVIEW_DEBOUNCE_MS = 500;
const SAMPLE_SOURCE = "sample";

interface FormValues {
  name: string;
  description?: string;
  title_template: string;
  body_template: string;
  body_html_template?: string;
}

function toBody(values: FormValues): TemplateWriteInput {
  return {
    name: values.name,
    description: values.description || undefined,
    kind: "alert",
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

export default function TemplateEditor() {
  const { id } = useParams<{ id: string }>();
  const isEdit = !!id;
  const navigate = useNavigate();
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

  const templateQuery = useQuery({
    queryKey: ["template", id],
    queryFn: () => getTemplate(Number(id)),
    enabled: isEdit,
  });

  const variablesQuery = useQuery({
    queryKey: ["template-variables"],
    queryFn: listTemplateVariables,
  });

  const recentEventsQuery = useQuery({
    queryKey: ["template-preview-events", teamId],
    queryFn: () => getAlertHistory({ teamId, includeTest: true, pageSize: 20 }),
    enabled: !!teamId,
  });

  useEffect(() => {
    if (!templateQuery.data) return;
    const t = templateQuery.data;
    form.setFieldsValue({
      name: t.name,
      description: t.description ?? undefined,
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
            setPreviewError(err instanceof ApiError ? err.detail : "미리보기에 실패했습니다");
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
  }, [titleValue, bodyValue, bodyHtmlValue, previewSource]);

  const saveMutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = toBody(values);
      return isEdit ? updateTemplate(Number(id), body) : createTemplate(teamId!, body);
    },
    onSuccess: () => {
      message.success(isEdit ? "템플릿이 수정되었습니다" : "템플릿이 생성되었습니다");
      queryClient.invalidateQueries({ queryKey: ["templates", teamId] });
      navigate("/templates");
    },
    onError: (err) => {
      setServerError(
        err instanceof ApiError
          ? err.detail
          : isEdit
            ? "템플릿 수정에 실패했습니다"
            : "템플릿 생성에 실패했습니다",
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
    return (
      <div>
        <h2>{isEdit ? "템플릿 수정" : "템플릿 생성"}</h2>
        <Alert type="info" showIcon message="소속된 팀이 없습니다" />
      </div>
    );
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
    <div style={{ display: "flex", gap: 24, alignItems: "flex-start", maxWidth: 1280 }}>
      <div style={{ flex: "1 1 640px", minWidth: 480 }}>
        <h2>{isEdit ? `템플릿 수정 — ${templateQuery.data?.name ?? ""}` : "템플릿 생성"}</h2>

        {serverError && (
          <Alert type="error" showIcon message={serverError} style={{ marginBottom: 16 }} />
        )}

        <Form<FormValues> form={form} layout="vertical" onFinish={handleFinish}>
          <Form.Item
            name="name"
            label="이름"
            rules={[{ required: true, message: "이름을 입력하세요" }]}
          >
            <Input placeholder="critical-alert-template" />
          </Form.Item>
          <Form.Item name="description" label="설명">
            <Input placeholder="온콜 팀 전용 알림 형식" />
          </Form.Item>

          <Form.Item
            name="title_template"
            label="제목 (title)"
            rules={[{ required: true, message: "제목 템플릿을 입력하세요" }]}
            help={errorsBySlot.get("title")?.map((e, i) => (
              <Text type="danger" key={i} style={{ display: "block" }}>
                {e.lineno != null ? `${e.lineno}행: ` : ""}
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
            label="본문 (body, 텍스트)"
            rules={[{ required: true, message: "본문 템플릿을 입력하세요" }]}
            help={errorsBySlot.get("body")?.map((e, i) => (
              <Text type="danger" key={i} style={{ display: "block" }}>
                {e.lineno != null ? `${e.lineno}행: ` : ""}
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
            label="본문 (body_html, 선택)"
            help={errorsBySlot.get("body_html")?.map((e, i) => (
              <Text type="danger" key={i} style={{ display: "block" }}>
                {e.lineno != null ? `${e.lineno}행: ` : ""}
                {e.message}
              </Text>
            ))}
          >
            <Input.TextArea
              id={BODY_HTML_FIELD_ID}
              rows={8}
              style={{ fontFamily: "monospace" }}
              placeholder="비워두면 이 슬롯은 렌더링되지 않습니다"
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
              저장
            </Button>
            <Button onClick={() => navigate("/templates")}>취소</Button>
          </Space>
        </Form>
      </div>

      <div style={{ flex: "0 0 360px", minWidth: 320 }}>
        <Title level={5}>변수 레퍼런스</Title>
        <Paragraph type="secondary" style={{ marginTop: -8 }}>
          클릭하면 현재 커서 위치에 삽입됩니다.
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

        <Title level={5}>실시간 미리보기</Title>
        <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }}>
          <Select
            style={{ width: "100%" }}
            value={previewSource}
            onChange={setPreviewSource}
            options={[{ value: SAMPLE_SOURCE, label: "샘플 알럿" }, ...eventOptions]}
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
            message="미정의 변수"
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
                  제목
                </Text>
                <div style={{ fontWeight: 600 }}>{previewResult.rendered.title}</div>
              </div>
              <div style={{ marginBottom: previewResult.rendered.body_html ? 8 : 0 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  본문
                </Text>
                <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontFamily: "monospace" }}>
                  {previewResult.rendered.body}
                </pre>
              </div>
              {previewResult.rendered.body_html && (
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    본문 (body_html, HTML 소스 -- 렌더링 없이 텍스트로 표시)
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
                ? "템플릿 오류로 미리보기를 표시할 수 없습니다."
                : "제목/본문을 입력하면 미리보기가 표시됩니다."}
            </Text>
          )}
        </div>
      </div>
    </div>
  );
}
