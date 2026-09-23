import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router";
import { Alert, App, Button, Drawer, Form, Input, Segmented, Select, Space, Typography } from "antd";
import { useTeam } from "../auth/TeamContext";
import { useDefaultCluster } from "../api/useDefaultCluster";
import { ApiError } from "../api/client";
import { createRule, getRule, updateRule, validateExpr } from "../api/rules";
import type { RuleMode, RuleWriteInput, Severity } from "../api/rules";
import ThresholdBuilder from "../components/rule-editor/ThresholdBuilder";
import PreviewChart from "../components/rule-editor/PreviewChart";
import { emptyBuilderState, generateBuilderExpr, generateSelector } from "../components/rule-editor/builderExpr";
import type { BuilderState } from "../components/rule-editor/builderExpr";

const { Text, Title } = Typography;

const SEVERITY_OPTIONS: { value: Severity; label: string }[] = [
  { value: "critical", label: "critical" },
  { value: "warning", label: "warning" },
  { value: "info", label: "info" },
];

const MODE_OPTIONS: { value: RuleMode; label: string }[] = [
  { value: "builder", label: "임계값 빌더" },
  { value: "promql", label: "PromQL" },
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

/** Drops incomplete label-filter rows (no key chosen yet) before the
 * builder state is either previewed or sent to the backend -- the backend
 * rejects a label filter with an empty key outright. */
function cleanBuilderState(state: BuilderState): BuilderState {
  return { ...state, labels: state.labels.filter((l) => l.key) };
}

function renderYamlPreview(
  values: FormValues,
  expr: string,
  mode: RuleMode,
  builderState: BuilderState,
  teamId: number,
  teamSlug: string,
): string {
  const labels = toRecord(values.labels);
  const annotations = toRecord(values.annotations);
  if (values.runbook_url) annotations.runbook_url = values.runbook_url;
  if (values.grafana_url) annotations["kam.io/grafana-url"] = values.grafana_url;
  if (mode === "builder") {
    annotations["kam.io/builder-v1"] = JSON.stringify(cleanBuilderState(builderState));
  }
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
    `          expr: ${expr || "<expr>"}`,
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
  const [previewValues, setPreviewValues] = useState<
    (FormValues & { expr: string; mode: RuleMode; builderState: BuilderState }) | null
  >(null);

  const [mode, setMode] = useState<RuleMode>("builder");
  const [builderState, setBuilderState] = useState<BuilderState>(emptyBuilderState());
  // The PromQL-mode textarea's own value. Kept around even while in
  // builder mode so re-entering builder mode can detect a hand-edit that
  // diverged from what the builder would generate (see `diverged` below).
  const [promqlExpr, setPromqlExpr] = useState("");

  const teamId = currentTeam?.id;
  const clusterId = cluster?.id;

  const builderGeneratedExpr = generateBuilderExpr(cleanBuilderState(builderState));
  const currentExpr = mode === "builder" ? builderGeneratedExpr : promqlExpr;
  const chartExpr = mode === "builder" ? generateSelector(cleanBuilderState(builderState)) : promqlExpr;
  const diverged = mode === "builder" && promqlExpr !== "" && promqlExpr !== builderGeneratedExpr;

  const ruleQuery = useQuery({
    queryKey: ["rule", teamId, clusterId, slug],
    queryFn: () => getRule(teamId!, clusterId!, slug!),
    enabled: isEdit && !!teamId && !!clusterId,
  });

  useEffect(() => {
    if (!ruleQuery.data) return;
    const data = ruleQuery.data;
    form.setFieldsValue({
      slug: data.slug,
      alert_name: data.alert_name,
      for: data.for ?? undefined,
      severity: data.severity as Severity,
      labels: fromRecord(data.labels),
      annotations: fromRecord(data.annotations),
      runbook_url: data.runbook_url ?? undefined,
      grafana_url: data.grafana_url ?? undefined,
    });
    setPromqlExpr(data.expr);
    if (data.mode === "builder" && data.builder_state) {
      setBuilderState(data.builder_state);
      setMode("builder");
    } else {
      setBuilderState(emptyBuilderState());
      setMode("promql");
    }
  }, [ruleQuery.data, form]);

  const handleModeChange = (nextMode: RuleMode) => {
    if (nextMode === "promql" && mode === "builder") {
      // Carry the builder-generated expression into the textarea so
      // switching modes doesn't silently discard what was just composed.
      setPromqlExpr(builderGeneratedExpr);
    }
    setExprValidation(null);
    setMode(nextMode);
  };

  const buildBody = (values: FormValues): RuleWriteInput => ({
    slug: values.slug,
    alert_name: values.alert_name,
    expr: currentExpr,
    for: values.for || undefined,
    severity: values.severity,
    labels: toRecord(values.labels),
    annotations: toRecord(values.annotations),
    runbook_url: values.runbook_url || undefined,
    grafana_url: values.grafana_url || undefined,
    mode,
    builder_state: mode === "builder" ? cleanBuilderState(builderState) : undefined,
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
        setServerError(err.detail);
        return;
      }
      setServerError(
        err instanceof ApiError ? err.detail : isEdit ? "룰 수정에 실패했습니다" : "룰 생성에 실패했습니다",
      );
    },
  });

  const handleValidate = async () => {
    if (!clusterId || !currentExpr) return;
    setValidating(true);
    try {
      const result = await validateExpr(clusterId, currentExpr);
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

  const handleFinish = (values: FormValues) => {
    setServerError(null);
    if (mode === "builder" && !builderState.metric) {
      setServerError("메트릭을 선택하세요");
      return;
    }
    if (mode === "promql" && !promqlExpr.trim()) {
      setServerError("표현식을 입력하세요");
      return;
    }
    saveMutation.mutate(values);
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
    <div style={{ maxWidth: 800 }}>
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
            setPreviewValues({
              ...(form.getFieldsValue(true) as FormValues),
              expr: currentExpr,
              mode,
              builderState,
            });
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
        onFinish={handleFinish}
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

        <Form.Item label="표현식 작성 방식">
          <Segmented value={mode} onChange={(v) => handleModeChange(v as RuleMode)} options={MODE_OPTIONS} />
        </Form.Item>

        {mode === "builder" && diverged && (
          <Alert
            type="warning"
            showIcon
            message="직접 수정된 표현식 — 빌더 상태와 불일치"
            description="PromQL 모드에서 표현식을 직접 수정했습니다. 저장 시 아래 빌더 상태로 생성된 표현식이 사용됩니다."
            style={{ marginBottom: 16 }}
          />
        )}

        {mode === "builder" ? (
          <ThresholdBuilder clusterId={clusterId} value={builderState} onChange={setBuilderState} />
        ) : (
          <Form.Item label="PromQL 표현식" required>
            <Input.TextArea
              rows={3}
              style={{ fontFamily: "monospace" }}
              value={promqlExpr}
              onChange={(e) => {
                setPromqlExpr(e.target.value);
                setExprValidation(null);
              }}
            />
          </Form.Item>
        )}

        <Space style={{ marginBottom: 16 }}>
          <Button onClick={handleValidate} loading={validating} disabled={!currentExpr}>
            검증
          </Button>
          {exprValidation &&
            (exprValidation.valid ? (
              <Text type="success">유효한 표현식입니다</Text>
            ) : (
              <Text type="danger">{exprValidation.error}</Text>
            ))}
        </Space>

        <Title level={5}>미리보기</Title>
        <div style={{ marginBottom: 24 }}>
          <PreviewChart
            clusterId={clusterId}
            chartExpr={chartExpr}
            fullExpr={currentExpr}
            threshold={mode === "builder" ? builderState.threshold : undefined}
          />
        </div>

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
          {previewValues && currentTeam
            ? renderYamlPreview(
                previewValues,
                previewValues.expr,
                previewValues.mode,
                previewValues.builderState,
                currentTeam.id,
                currentTeam.slug,
              )
            : ""}
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
