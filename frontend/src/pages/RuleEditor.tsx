import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router";
import { Alert, App, Button, Drawer, Form, Input, Select, Space, Typography } from "antd";
import { useTeam } from "../auth/TeamContext";
import { useDefaultCluster } from "../api/useDefaultCluster";
import { ApiError } from "../api/client";
import { createRule, getRule, updateRule, validateExpr } from "../api/rules";
import type { RuleWriteInput, Severity } from "../api/rules";

const { Text, Title } = Typography;

const SEVERITY_OPTIONS: { value: Severity; label: string }[] = [
  { value: "critical", label: "critical" },
  { value: "warning", label: "warning" },
  { value: "info", label: "info" },
];

// No leading or trailing hyphen, max 63 chars -- mirrors backend RULE_SLUG_RE.
const SLUG_PATTERN = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const ALERT_NAME_PATTERN = /^[a-zA-Z_][a-zA-Z0-9_]*$/;

interface KeyValue {
  key: string;
  value: string;
}

interface ValidateStatus {
  valid: boolean;
  error: string | null;
}

interface FormValues {
  slug: string;
  alert_name: string;
  expr: string;
  for?: string;
  severity: Severity;
  labels?: KeyValue[];
  annotations?: KeyValue[];
  runbook_url?: string;
  grafana_url?: string;
}

function toRecord(list?: KeyValue[]): Record<string, string> {
  const result: Record<string, string> = {};
  for (const item of list ?? []) {
    if (item?.key) result[item.key] = item.value ?? "";
  }
  return result;
}

function fromRecord(record: Record<string, string>): KeyValue[] {
  return Object.entries(record).map(([key, value]) => ({ key, value }));
}

function renderYamlPreview(values: FormValues, teamId: number, teamSlug: string): string {
  const labels = toRecord(values.labels);
  const annotations = toRecord(values.annotations);
  if (values.runbook_url) annotations.runbook_url = values.runbook_url;
  if (values.grafana_url) annotations["kam.io/grafana-url"] = values.grafana_url;
  labels.kam_team = teamSlug;
  labels.severity = values.severity ?? "";

  const lines = [
    "apiVersion: monitoring.coreos.com/v1",
    "kind: PrometheusRule",
    "metadata:",
    // Keyed by team id, not slug -- two teams' slugs could otherwise
    // collide at a hyphen boundary (see backend rule_object_name()).
    `  name: kam-t${teamId}-${values.slug || "<slug>"}`,
    "  labels:",
    "    app.kubernetes.io/managed-by: kam",
    `    kam/team-slug: ${teamSlug}`,
    "spec:",
    "  groups:",
    `    - name: kam-${teamSlug}`,
    "      rules:",
    `        - alert: ${values.alert_name || "<alert_name>"}`,
    `          expr: ${values.expr || "<expr>"}`,
  ];
  if (values.for) lines.push(`          for: ${values.for}`);
  lines.push("          labels:");
  for (const [k, v] of Object.entries(labels)) {
    lines.push(`            ${k}: ${v}`);
  }
  if (Object.keys(annotations).length > 0) {
    lines.push("          annotations:");
    for (const [k, v] of Object.entries(annotations)) {
      lines.push(`            ${k}: ${v}`);
    }
  }
  return lines.join("\n");
}

export default function RuleEditor() {
  const { slug } = useParams<{ slug: string }>();
  const isEdit = !!slug;
  const navigate = useNavigate();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { currentTeam } = useTeam();
  const { cluster } = useDefaultCluster();
  const [form] = Form.useForm<FormValues>();
  const [serverError, setServerError] = useState<string | null>(null);
  const [exprValidation, setExprValidation] = useState<ValidateStatus | null>(null);
  const [validating, setValidating] = useState(false);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewValues, setPreviewValues] = useState<FormValues | null>(null);

  const teamId = currentTeam?.id;
  const clusterId = cluster?.id;

  const ruleQuery = useQuery({
    queryKey: ["rule", teamId, clusterId, slug],
    queryFn: () => getRule(teamId!, clusterId!, slug!),
    enabled: isEdit && !!teamId && !!clusterId,
  });

  useEffect(() => {
    if (ruleQuery.data) {
      form.setFieldsValue({
        slug: ruleQuery.data.slug,
        alert_name: ruleQuery.data.alert_name,
        expr: ruleQuery.data.expr,
        for: ruleQuery.data.for ?? undefined,
        severity: ruleQuery.data.severity as Severity,
        labels: fromRecord(ruleQuery.data.labels),
        annotations: fromRecord(ruleQuery.data.annotations),
        runbook_url: ruleQuery.data.runbook_url ?? undefined,
        grafana_url: ruleQuery.data.grafana_url ?? undefined,
      });
    }
  }, [ruleQuery.data, form]);

  const buildBody = (values: FormValues): RuleWriteInput => ({
    slug: values.slug,
    alert_name: values.alert_name,
    expr: values.expr,
    for: values.for || undefined,
    severity: values.severity,
    labels: toRecord(values.labels),
    annotations: toRecord(values.annotations),
    runbook_url: values.runbook_url || undefined,
    grafana_url: values.grafana_url || undefined,
  });

  const saveMutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = buildBody(values);
      return isEdit
        ? updateRule(teamId!, clusterId!, slug!, body)
        : createRule(teamId!, clusterId!, body);
    },
    onSuccess: () => {
      message.success(isEdit ? "룰이 수정되었습니다" : "룰이 생성되었습니다");
      queryClient.invalidateQueries({ queryKey: ["rules", teamId, clusterId] });
      navigate("/rules");
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 422) {
        form.setFields([{ name: "expr", errors: [err.detail] }]);
        return;
      }
      setServerError(
        err instanceof ApiError ? err.detail : isEdit ? "룰 수정에 실패했습니다" : "룰 생성에 실패했습니다",
      );
    },
  });

  const handleValidate = async () => {
    if (!clusterId) return;
    const expr = form.getFieldValue("expr") as string | undefined;
    if (!expr) return;
    setValidating(true);
    try {
      const result = await validateExpr(clusterId, expr);
      setExprValidation(result);
    } catch (err) {
      setExprValidation({
        valid: false,
        error: err instanceof ApiError ? err.detail : "검증에 실패했습니다",
      });
    } finally {
      setValidating(false);
    }
  };

  if (!currentTeam) {
    return (
      <div>
        <h2>{isEdit ? "룰 수정" : "룰 생성"}</h2>
        <Alert type="info" showIcon message="소속된 팀이 없습니다" />
      </div>
    );
  }

  return (
    <div style={{ maxWidth: 720 }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <h2 style={{ margin: 0 }}>{isEdit ? `룰 수정 — ${slug}` : "룰 생성"}</h2>
        <Button
          onClick={() => {
            setPreviewValues(form.getFieldsValue(true) as FormValues);
            setPreviewOpen(true);
          }}
        >
          YAML 미리보기
        </Button>
      </div>

      {serverError && (
        <Alert type="error" showIcon message={serverError} style={{ marginBottom: 16 }} />
      )}

      <Form<FormValues>
        form={form}
        layout="vertical"
        initialValues={{ severity: "warning", labels: [], annotations: [] }}
        onFinish={(values) => {
          setServerError(null);
          saveMutation.mutate(values);
        }}
      >
        <Form.Item
          name="slug"
          label="슬러그"
          rules={[
            { required: true, message: "슬러그를 입력하세요" },
            { pattern: SLUG_PATTERN, message: "소문자/숫자/하이픈만 사용할 수 있습니다" },
          ]}
        >
          <Input disabled={isEdit} placeholder="high-cpu" />
        </Form.Item>

        <Form.Item
          name="alert_name"
          label="알럿명"
          rules={[
            { required: true, message: "알럿명을 입력하세요" },
            {
              pattern: ALERT_NAME_PATTERN,
              message: "영문/숫자/밑줄만 사용할 수 있으며 숫자로 시작할 수 없습니다",
            },
          ]}
        >
          <Input placeholder="HighCpuUsage" />
        </Form.Item>

        <Form.Item name="severity" label="심각도" rules={[{ required: true }]}>
          <Select options={SEVERITY_OPTIONS} />
        </Form.Item>

        <Form.Item
          name="expr"
          label="PromQL 표현식"
          rules={[{ required: true, message: "표현식을 입력하세요" }]}
        >
          <Input.TextArea
            rows={3}
            style={{ fontFamily: "monospace" }}
            onChange={() => setExprValidation(null)}
          />
        </Form.Item>
        <Space style={{ marginBottom: 16 }}>
          <Button onClick={handleValidate} loading={validating}>
            검증
          </Button>
          {exprValidation &&
            (exprValidation.valid ? (
              <Text type="success">유효한 표현식입니다</Text>
            ) : (
              <Text type="danger">{exprValidation.error}</Text>
            ))}
        </Space>

        <Form.Item name="for" label="for" help='기간 형식, 예: "5m"'>
          <Input placeholder="5m" />
        </Form.Item>

        <Title level={5}>레이블</Title>
        <KeyValueList name="labels" addLabel="레이블 추가" />

        <Title level={5}>어노테이션</Title>
        <KeyValueList name="annotations" addLabel="어노테이션 추가" />

        <Form.Item name="runbook_url" label="Runbook URL">
          <Input placeholder="https://runbooks.example.com/..." />
        </Form.Item>
        <Form.Item name="grafana_url" label="Grafana URL">
          <Input placeholder="https://grafana.example.com/d/..." />
        </Form.Item>

        <Space>
          <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>
            저장
          </Button>
          <Button onClick={() => navigate("/rules")}>취소</Button>
        </Space>
      </Form>

      <Drawer
        title="YAML 미리보기"
        open={previewOpen}
        onClose={() => setPreviewOpen(false)}
        width={480}
      >
        <pre style={{ whiteSpace: "pre-wrap", fontFamily: "monospace", fontSize: 12 }}>
          {previewValues ? renderYamlPreview(previewValues, currentTeam.id, currentTeam.slug) : ""}
        </pre>
      </Drawer>
    </div>
  );
}

function KeyValueList({ name, addLabel }: { name: "labels" | "annotations"; addLabel: string }) {
  return (
    <Form.List name={name}>
      {(fields, { add, remove }) => (
        <>
          {fields.map((field) => (
            <Space key={field.key} style={{ display: "flex", marginBottom: 8 }} align="baseline">
              <Form.Item
                name={[field.name, "key"]}
                rules={[{ required: true, message: "키를 입력하세요" }]}
                noStyle
              >
                <Input placeholder="key" />
              </Form.Item>
              <Form.Item
                name={[field.name, "value"]}
                rules={[{ required: true, message: "값을 입력하세요" }]}
                noStyle
              >
                <Input placeholder="value" />
              </Form.Item>
              <Button danger onClick={() => remove(field.name)}>
                삭제
              </Button>
            </Space>
          ))}
          <Button onClick={() => add()} style={{ marginBottom: 16 }}>
            {addLabel}
          </Button>
        </>
      )}
    </Form.List>
  );
}
