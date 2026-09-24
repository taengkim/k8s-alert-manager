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
  InputNumber,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import { useTeam } from "../auth/TeamContext";
import { useDefaultCluster } from "../api/useDefaultCluster";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { listChannels, listEscalationTargets } from "../api/channels";
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
import MatcherListEditor from "../components/MatcherListEditor";

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
  include_shared: boolean;
  escalation_enabled: boolean;
  escalation_after_minutes?: number;
  escalation_channel_ids: number[];
  renotify_interval_minutes?: number;
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
    include_shared: values.include_shared ?? false,
    escalation_enabled: values.action === "notify" ? (values.escalation_enabled ?? false) : false,
    escalation_after_minutes:
      values.action === "notify" && values.escalation_enabled
        ? values.escalation_after_minutes
        : undefined,
    escalation_channel_ids:
      values.action === "notify" && values.escalation_enabled
        ? (values.escalation_channel_ids ?? [])
        : [],
    renotify_interval_minutes:
      values.action === "notify" ? values.renotify_interval_minutes : undefined,
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
  const escalationEnabled = Form.useWatch("escalation_enabled", form) ?? false;

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
  const escalationTargetsQuery = useQuery({
    queryKey: ["escalation-targets", teamId],
    queryFn: () => listEscalationTargets(teamId!),
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
      include_shared: r.include_shared,
      escalation_enabled: r.escalation_enabled,
      escalation_after_minutes: r.escalation_after_minutes ?? undefined,
      escalation_channel_ids: r.escalation_channel_ids,
      renotify_interval_minutes: r.renotify_interval_minutes ?? undefined,
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
  const escalationChannelOptions = (escalationTargetsQuery.data ?? []).map((c) => ({
    value: c.id,
    label: c.team_slug === currentTeam.slug ? c.name : `${c.name} (${c.team_slug})`,
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
          include_shared: false,
          escalation_enabled: false,
          escalation_channel_ids: [],
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

        <Form.Item
          name="include_shared"
          label="공유 알럿 포함"
          valuePropName="checked"
          help="받는 공유(view_notify)의 알럿도 이 규칙으로 알림"
        >
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
        <MatcherListEditor name="matchers" form={form} />

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

            <Form.Item
              name="renotify_interval_minutes"
              label="미해결 재알림 간격 (분)"
              help="설정하면 이 알럿이 firing 상태로 미확인(unack)인 동안 지정한 간격마다 같은 채널로 반복 알림을 보냅니다. 비워두면 재알림하지 않습니다."
            >
              <InputNumber min={1} style={{ width: 200 }} placeholder="예: 15" />
            </Form.Item>

            <Title level={5}>에스컬레이션</Title>
            <Form.Item
              name="escalation_enabled"
              label="에스컬레이션 사용"
              valuePropName="checked"
              help="firing 상태로 지정한 시간이 지나도 확인(ack)되지 않으면 에스컬레이션 채널로 추가 알림을 보냅니다."
            >
              <Switch />
            </Form.Item>
            {escalationEnabled && (
              <>
                <Form.Item
                  name="escalation_after_minutes"
                  label="에스컬레이션 대기 시간 (분)"
                  rules={[{ required: true, message: "에스컬레이션 대기 시간을 입력하세요" }]}
                >
                  <InputNumber min={1} style={{ width: 200 }} placeholder="예: 10" />
                </Form.Item>
                <Form.Item
                  name="escalation_channel_ids"
                  label="에스컬레이션 채널"
                  help="같은 팀의 채널 또는 '타팀 에스컬레이션 허용'이 켜진 다른 팀의 채널을 선택할 수 있습니다."
                  rules={[{ required: true, message: "에스컬레이션 채널을 하나 이상 선택하세요" }]}
                >
                  <Select
                    mode="multiple"
                    loading={escalationTargetsQuery.isLoading}
                    options={escalationChannelOptions}
                    placeholder="에스컬레이션 채널 선택"
                  />
                </Form.Item>
              </>
            )}
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
