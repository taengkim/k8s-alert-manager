import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Form,
  Input,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import type { FormInstance } from "antd";
import { useTeam } from "../auth/TeamContext";
import { useDefaultCluster } from "../api/useDefaultCluster";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { listChannels } from "../api/channels";
import {
  createRoute,
  getRoute,
  listNamespaces,
  previewRoute,
  updateRoute,
} from "../api/routes";
import type {
  MatcherKind,
  MatcherTarget,
  RouteAction,
  RoutePreviewItem,
  RouteVerdict,
  RouteWriteInput,
} from "../api/routes";
import { listTemplates } from "../api/templates";
import TemplatePreviewPopover from "../components/TemplatePreviewPopover";

const { Text, Title } = Typography;

const ACTION_OPTIONS: { value: RouteAction; label: string }[] = [
  { value: "notify", label: "알림" },
  { value: "suppress", label: "차단" },
];

const SEVERITY_OPTIONS = [
  { value: "critical", label: "critical" },
  { value: "warning", label: "warning" },
  { value: "info", label: "info" },
  { value: "none", label: "없음" },
];

const MATCHER_KIND_OPTIONS: { value: MatcherKind; label: string }[] = [
  { value: "include", label: "포함" },
  { value: "exclude", label: "제외" },
];

const MATCHER_TARGET_OPTIONS: { value: MatcherTarget; label: string }[] = [
  { value: "alertname", label: "알럿명" },
  { value: "label", label: "레이블" },
  { value: "annotation", label: "어노테이션" },
];

// Preview only cares about what feeds evaluate() -- not channel_ids (which
// preview never uses), so clicking "최근 알럿에 테스트" shouldn't be blocked
// by "채널을 하나 이상 선택하세요" while a draft's filters are still being
// worked out. "name" stays in this list because the backend's RouteWrite
// schema requires it even for a preview-only draft.
const PREVIEW_VALIDATE_FIELDS = [
  "name",
  "action",
  "severities",
  "namespaces_include",
  "namespaces_exclude",
  "clusters",
  "matchers",
];

const VERDICT_LABEL: Record<RouteVerdict, string> = {
  matched: "일치",
  cluster_filtered: "클러스터 필터링됨",
  gated: "비활성화 / 트리거 불일치",
  severity_filtered: "심각도 필터링됨",
  namespace_filtered: "네임스페이스 필터링됨",
  not_included: "포함 조건 불일치",
  excluded: "제외 조건에 매치",
};

const VERDICT_COLOR: Record<RouteVerdict, string> = {
  matched: "green",
  cluster_filtered: "default",
  gated: "default",
  severity_filtered: "default",
  namespace_filtered: "default",
  not_included: "default",
  excluded: "red",
};

interface MatcherFormValue {
  kind: MatcherKind;
  target: MatcherTarget;
  key?: string;
  pattern: string;
}

interface FormValues {
  name: string;
  description?: string;
  action: RouteAction;
  enabled: boolean;
  notify_on_firing: boolean;
  notify_on_resolved: boolean;
  severities: string[];
  namespaces_include: string[];
  namespaces_exclude: string[];
  clusters: number[];
  channel_ids: number[];
  matchers: MatcherFormValue[];
  template_id?: number;
}

function isValidRegex(pattern: string): boolean {
  try {
    // eslint-disable-next-line no-new
    new RegExp(pattern);
    return true;
  } catch {
    return false;
  }
}

function toBody(values: FormValues): RouteWriteInput {
  return {
    name: values.name,
    description: values.description || undefined,
    action: values.action,
    enabled: values.enabled,
    notify_on_firing: values.notify_on_firing,
    notify_on_resolved: values.notify_on_resolved,
    severities: values.severities?.length ? values.severities : undefined,
    namespaces_include: values.namespaces_include?.length ? values.namespaces_include : undefined,
    namespaces_exclude: values.namespaces_exclude?.length ? values.namespaces_exclude : undefined,
    clusters: values.clusters?.length ? values.clusters : undefined,
    channel_ids: values.action === "suppress" ? [] : (values.channel_ids ?? []),
    template_id: values.action === "suppress" ? undefined : values.template_id,
    matchers: (values.matchers ?? []).map((m) => ({
      kind: m.kind,
      target: m.target,
      key: m.target === "alertname" ? undefined : m.key,
      pattern: m.pattern,
    })),
  };
}

export default function RouteEditor() {
  const { id } = useParams<{ id: string }>();
  const isEdit = !!id;
  const navigate = useNavigate();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { currentTeam } = useTeam();
  const { cluster } = useDefaultCluster();
  const [form] = Form.useForm<FormValues>();
  const [serverError, setServerError] = useState<string | null>(null);
  const [previewResults, setPreviewResults] = useState<RoutePreviewItem[] | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const teamId = currentTeam?.id;
  const clusterId = cluster?.id;
  const action = Form.useWatch("action", form) ?? "notify";

  const routeQuery = useQuery({
    queryKey: ["route", id],
    queryFn: () => getRoute(Number(id)),
    enabled: isEdit,
  });

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const channelsQuery = useQuery({
    queryKey: ["channels", teamId],
    queryFn: () => listChannels(teamId!),
    enabled: !!teamId,
  });
  const namespacesQuery = useQuery({
    queryKey: ["namespaces", clusterId],
    queryFn: () => listNamespaces(clusterId!),
    enabled: !!clusterId,
  });
  const templatesQuery = useQuery({
    queryKey: ["templates", teamId],
    queryFn: () => listTemplates(teamId!),
    enabled: !!teamId,
  });
  const selectedTemplateId = Form.useWatch("template_id", form);
  const selectedTemplate = (templatesQuery.data ?? []).find((t) => t.id === selectedTemplateId);

  useEffect(() => {
    if (!routeQuery.data) return;
    const r = routeQuery.data;
    form.setFieldsValue({
      name: r.name,
      description: r.description ?? undefined,
      action: r.action,
      enabled: r.enabled,
      notify_on_firing: r.notify_on_firing,
      notify_on_resolved: r.notify_on_resolved,
      severities: r.severities ?? [],
      namespaces_include: r.namespaces_include ?? [],
      namespaces_exclude: r.namespaces_exclude ?? [],
      clusters: r.clusters ?? [],
      channel_ids: r.channel_ids,
      template_id: r.template_id ?? undefined,
      matchers: r.matchers.map((m) => ({
        kind: m.kind,
        target: m.target,
        key: m.key ?? undefined,
        pattern: m.pattern,
      })),
    });
  }, [routeQuery.data, form]);

  const saveMutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = toBody(values);
      return isEdit ? updateRoute(Number(id), body) : createRoute(teamId!, body);
    },
    onSuccess: () => {
      message.success(isEdit ? "규칙이 수정되었습니다" : "규칙이 생성되었습니다");
      queryClient.invalidateQueries({ queryKey: ["routes", teamId] });
      navigate("/routes");
    },
    onError: (err) => {
      setServerError(
        err instanceof ApiError
          ? err.detail
          : isEdit
            ? "규칙 수정에 실패했습니다"
            : "규칙 생성에 실패했습니다",
      );
    },
  });

  const handlePreview = async () => {
    if (!teamId) return;
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      // Only the fields evaluate() actually reads are validated -- e.g. an
      // incomplete channel_ids selection shouldn't block trying a draft's
      // filters out. validateFields(subset) only returns that subset, so
      // the full current form state is read separately via getFieldsValue.
      await form.validateFields(PREVIEW_VALIDATE_FIELDS);
      const values = form.getFieldsValue(true) as FormValues;
      const results = await previewRoute(teamId, toBody(values));
      setPreviewResults(results);
    } catch (err) {
      if (err instanceof ApiError) {
        setPreviewError(err.detail);
      } else if (!(err && typeof err === "object" && "errorFields" in err)) {
        setPreviewError("미리보기에 실패했습니다");
      }
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleFinish = (values: FormValues) => {
    setServerError(null);
    saveMutation.mutate(values);
  };

  if (!currentTeam) {
    return (
      <div>
        <h2>{isEdit ? "규칙 수정" : "규칙 생성"}</h2>
        <Alert type="info" showIcon message="소속된 팀이 없습니다" />
      </div>
    );
  }

  const channelOptions = (channelsQuery.data ?? []).map((c) => ({
    value: c.id,
    label: c.enabled ? c.name : `${c.name} (비활성)`,
  }));
  const clusterOptions = (clustersQuery.data ?? []).map((c) => ({
    value: c.id,
    label: c.display_name,
  }));
  const namespaceOptions = (namespacesQuery.data ?? []).map((ns) => ({ value: ns, label: ns }));

  const previewColumns = [
    { title: "알럿명", dataIndex: "alertname", key: "alertname" },
    {
      title: "심각도",
      dataIndex: "severity",
      key: "severity",
      render: (v: string | null) => v ?? "-",
    },
    {
      title: "네임스페이스",
      dataIndex: "namespace",
      key: "namespace",
      render: (v: string | null) => v ?? "-",
    },
    { title: "클러스터", dataIndex: "cluster", key: "cluster" },
    {
      title: "현재 상태",
      dataIndex: "status",
      key: "status",
      render: (v: string) => (v === "firing" ? "firing" : "resolved"),
    },
    {
      title: "판정",
      dataIndex: "verdict",
      key: "verdict",
      render: (verdict: RouteVerdict, row: RoutePreviewItem) => (
        <Space size={4}>
          <Tag color={VERDICT_COLOR[verdict]}>{VERDICT_LABEL[verdict]}</Tag>
          {row.blocking_matcher_position !== null && (
            <Text type="secondary">(매처 #{row.blocking_matcher_position + 1})</Text>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div style={{ maxWidth: 900 }}>
      <h2>{isEdit ? `규칙 수정 — ${routeQuery.data?.name ?? ""}` : "규칙 생성"}</h2>

      {serverError && (
        <Alert type="error" showIcon message={serverError} style={{ marginBottom: 16 }} />
      )}

      <Form<FormValues>
        form={form}
        layout="vertical"
        initialValues={{
          action: "notify",
          enabled: true,
          notify_on_firing: true,
          notify_on_resolved: false,
          severities: [],
          namespaces_include: [],
          namespaces_exclude: [],
          clusters: [],
          channel_ids: [],
          matchers: [],
        }}
        onFinish={handleFinish}
      >
        <Form.Item
          name="name"
          label="이름"
          rules={[{ required: true, message: "이름을 입력하세요" }]}
        >
          <Input placeholder="critical-to-oncall" />
        </Form.Item>
        <Form.Item name="description" label="설명">
          <Input.TextArea rows={2} />
        </Form.Item>

        <Form.Item name="action" label="액션">
          <Segmented options={ACTION_OPTIONS} />
        </Form.Item>

        <Form.Item name="enabled" label="활성화" valuePropName="checked">
          <Switch />
        </Form.Item>

        <Title level={5}>필터</Title>

        <Form.Item name="severities" label="심각도" help="비워두면 모든 심각도에 적용">
          <Checkbox.Group options={SEVERITY_OPTIONS} />
        </Form.Item>

        <Form.Item
          name="namespaces_include"
          label="네임스페이스 포함"
          help="정확일치 또는 정규식(전체일치) -- 비워두면 전체 허용"
        >
          <Select
            mode="tags"
            options={namespaceOptions}
            placeholder="네임스페이스 선택 또는 정규식 입력"
          />
        </Form.Item>
        <Form.Item
          name="namespaces_exclude"
          label="네임스페이스 제외"
          help="정확일치 또는 정규식(전체일치)"
        >
          <Select
            mode="tags"
            options={namespaceOptions}
            placeholder="네임스페이스 선택 또는 정규식 입력"
          />
        </Form.Item>

        <Form.Item name="clusters" label="클러스터" help="비워두면 전체 클러스터에 적용">
          <Select mode="multiple" options={clusterOptions} placeholder="전체 클러스터" />
        </Form.Item>

        <Title level={5}>매처</Title>
        <Form.List name="matchers">
          {(fields, { add, remove }) => (
            <>
              {fields.map((field) => (
                <MatcherRow
                  key={field.key}
                  field={field}
                  form={form}
                  onRemove={() => remove(field.name)}
                />
              ))}
              <Button
                onClick={() => add({ kind: "include", target: "alertname", pattern: "" })}
                style={{ marginBottom: 16 }}
              >
                매처 추가
              </Button>
            </>
          )}
        </Form.List>

        {action === "notify" && (
          <>
            <Title level={5}>알림 설정</Title>
            <Form.Item
              name="channel_ids"
              label="채널"
              rules={[{ required: true, message: "채널을 하나 이상 선택하세요" }]}
            >
              <Select mode="multiple" options={channelOptions} placeholder="채널 선택" />
            </Form.Item>
            <Form.Item
              name="template_id"
              label="메시지 템플릿"
              help="비워두면 채널의 템플릿(또는 채널 타입/앱 기본 템플릿)을 사용합니다"
            >
              <Select
                allowClear
                loading={templatesQuery.isLoading}
                placeholder="기본값 상속"
                options={(templatesQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
              />
            </Form.Item>
            <TemplatePreviewPopover template={selectedTemplate} />
            <Space direction="vertical" style={{ marginBottom: 16, marginTop: 8 }}>
              <Form.Item name="notify_on_firing" valuePropName="checked" noStyle>
                <Checkbox>firing 시 알림</Checkbox>
              </Form.Item>
              <Form.Item name="notify_on_resolved" valuePropName="checked" noStyle>
                <Checkbox>resolved 시 알림</Checkbox>
              </Form.Item>
            </Space>
          </>
        )}

        <Space>
          <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>
            저장
          </Button>
          <Button onClick={() => navigate("/routes")}>취소</Button>
        </Space>
      </Form>

      <Title level={5} style={{ marginTop: 32 }}>
        미리보기
      </Title>
      <Space direction="vertical" style={{ marginBottom: 16 }}>
        <Space>
          <Button onClick={() => void handlePreview()} loading={previewLoading}>
            최근 알럿에 테스트
          </Button>
          <Text type="secondary">최근 알럿 이력 최대 200건에 이 규칙을 평가합니다.</Text>
        </Space>
        <Text type="secondary">
          "현재 상태"는 이력에 기록된 실제 상태이며, 판정은 항상 이 알럿이 firing으로
          들어왔을 때를 기준으로 평가합니다.
        </Text>
      </Space>
      {previewError && (
        <Alert type="error" showIcon message={previewError} style={{ marginBottom: 16 }} />
      )}
      {previewResults && (
        <Table<RoutePreviewItem>
          rowKey="event_id"
          size="small"
          dataSource={previewResults}
          columns={previewColumns}
          pagination={{ pageSize: 20 }}
        />
      )}
    </div>
  );
}

function MatcherRow({
  field,
  form,
  onRemove,
}: {
  field: { key: number; name: number };
  form: FormInstance<FormValues>;
  onRemove: () => void;
}) {
  const target = Form.useWatch(["matchers", field.name, "target"], form) as
    | MatcherTarget
    | undefined;
  const needsKey = target === "label" || target === "annotation";

  return (
    <Space align="baseline" style={{ display: "flex", marginBottom: 8, flexWrap: "wrap" }}>
      <Form.Item name={[field.name, "kind"]} initialValue="include" noStyle>
        <Segmented options={MATCHER_KIND_OPTIONS} />
      </Form.Item>
      <Form.Item name={[field.name, "target"]} initialValue="alertname" noStyle>
        <Select style={{ width: 140 }} options={MATCHER_TARGET_OPTIONS} />
      </Form.Item>
      {needsKey && (
        <Form.Item
          name={[field.name, "key"]}
          rules={[{ required: true, message: "키를 입력하세요" }]}
          noStyle
        >
          <Input placeholder="key" style={{ width: 140 }} />
        </Form.Item>
      )}
      <Form.Item
        name={[field.name, "pattern"]}
        rules={[
          { required: true, message: "패턴을 입력하세요" },
          {
            validator: async (_, value?: string) => {
              if (value && !isValidRegex(value)) {
                throw new Error("올바른 정규식이 아닙니다");
              }
            },
          },
        ]}
        noStyle
      >
        <Input placeholder="정규식 패턴" style={{ width: 220 }} />
      </Form.Item>
      <Button danger onClick={onRemove}>
        삭제
      </Button>
    </Space>
  );
}
