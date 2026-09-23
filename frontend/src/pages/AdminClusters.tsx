import { useMemo, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import {
  Alert,
  App,
  Badge,
  Button,
  Divider,
  Drawer,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Switch,
  Table,
  Tooltip,
  Typography,
} from "antd";
import { ApiError } from "../api/client";
import {
  createCluster,
  deleteCluster,
  getClusterHealth,
  listClusters,
  updateCluster,
} from "../api/admin";
import { listTeams } from "../api/teams";
import type {
  Cluster,
  ClusterHealth,
  ClusterSecretReveal,
  ClusterWriteInput,
  ComponentHealth,
  K8sAuthKind,
} from "../api/types";

dayjs.extend(relativeTime);

const { Text } = Typography;

const AUTH_KIND_OPTIONS: { value: K8sAuthKind; label: string }[] = [
  { value: "incluster", label: "In-cluster" },
  { value: "kubeconfig", label: "kubeconfig" },
  { value: "token", label: "Token" },
];

const HEARTBEAT_STATE_COLOR: Record<string, string> = {
  ok: "green",
  late: "orange",
  missing: "red",
  unknown: "default",
};

const NAME_PATTERN = /^[a-z0-9][a-z0-9-]{1,62}$/;

interface ClusterFormValues {
  name: string;
  display_name: string;
  k8s_auth_kind: K8sAuthKind;
  k8s_api_url?: string;
  kubeconfig_yaml?: string;
  token?: string;
  ca_cert?: string;
  prometheus_url: string;
  alertmanager_url: string;
  grafana_url?: string;
  rules_namespace: string;
  heartbeat_enabled: boolean;
  heartbeat_alertname: string;
  heartbeat_timeout_seconds: number;
  heartbeat_team_id?: number;
}

function ComponentDot({ label, health }: { label: string; health?: ComponentHealth }) {
  if (!health) {
    return (
      <Tooltip title={`${label}: 알 수 없음`}>
        <Badge status="default" />
      </Tooltip>
    );
  }
  const title = health.ok
    ? `${label}: 정상${health.latency_ms != null ? ` (${health.latency_ms}ms)` : ""}`
    : `${label}: 실패${health.error ? ` — ${health.error}` : ""}`;
  return (
    <Tooltip title={title}>
      <Badge status={health.ok ? "success" : "error"} />
    </Tooltip>
  );
}

export default function AdminClusters() {
  const { message } = App.useApp();
  const queryClient = useQueryClient();

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const clusters = useMemo(() => clustersQuery.data ?? [], [clustersQuery.data]);
  const teamsQuery = useQuery({ queryKey: ["teams"], queryFn: listTeams });

  // One health poll per cluster, refetched every 30s -- mirrors the
  // ClusterHealthCache's own TTL server-side, so this page's dots stay
  // reasonably fresh without hammering every cluster's control plane on
  // every render.
  const healthQueries = useQueries({
    queries: clusters.map((cluster) => ({
      queryKey: ["cluster-health", cluster.id],
      queryFn: () => getClusterHealth(cluster.id),
      refetchInterval: 30_000,
    })),
  });
  const healthByClusterId = useMemo(() => {
    const map = new Map<number, ClusterHealth>();
    clusters.forEach((cluster, idx) => {
      const data = healthQueries[idx]?.data;
      if (data) map.set(cluster.id, data);
    });
    return map;
  }, [clusters, healthQueries]);

  const [drawerOpen, setDrawerOpen] = useState(false);
  const [editing, setEditing] = useState<Cluster | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [form] = Form.useForm<ClusterFormValues>();
  const authKind = Form.useWatch("k8s_auth_kind", form);
  const [secretReveal, setSecretReveal] = useState<ClusterSecretReveal | null>(null);

  const openCreate = () => {
    setEditing(null);
    setFormError(null);
    form.resetFields();
    form.setFieldsValue({
      k8s_auth_kind: "kubeconfig",
      rules_namespace: "kam-rules",
      heartbeat_enabled: true,
      heartbeat_alertname: "Watchdog",
      heartbeat_timeout_seconds: 600,
    });
    setDrawerOpen(true);
  };

  const openEdit = (cluster: Cluster) => {
    setEditing(cluster);
    setFormError(null);
    form.resetFields();
    form.setFieldsValue({
      name: cluster.name,
      display_name: cluster.display_name,
      k8s_auth_kind: cluster.k8s_auth_kind ?? "kubeconfig",
      k8s_api_url: cluster.k8s_api_url ?? undefined,
      prometheus_url: cluster.prometheus_url ?? "",
      alertmanager_url: cluster.alertmanager_url ?? "",
      grafana_url: cluster.grafana_url ?? undefined,
      rules_namespace: cluster.rules_namespace ?? "kam-rules",
      heartbeat_enabled: cluster.heartbeat_enabled ?? true,
      heartbeat_alertname: cluster.heartbeat_alertname ?? "Watchdog",
      heartbeat_timeout_seconds: cluster.heartbeat_timeout_seconds ?? 600,
      heartbeat_team_id: cluster.heartbeat_team_id ?? undefined,
    });
    setDrawerOpen(true);
  };

  const buildBody = (values: ClusterFormValues): ClusterWriteInput => {
    // A blank kubeconfig/token field means "leave the stored credentials
    // alone" on edit -- `credentials: undefined` is dropped by
    // JSON.stringify entirely, so the backend never sees the key and
    // leaves credentials_encrypted untouched (see ClusterUpdate's
    // model_fields_set handling). On create, "blank" simply means "no
    // credentials at all" (dev default / must be provided later).
    let credentials: ClusterWriteInput["credentials"];
    if (values.k8s_auth_kind === "kubeconfig") {
      credentials = values.kubeconfig_yaml ? values.kubeconfig_yaml : undefined;
    } else if (values.k8s_auth_kind === "token") {
      credentials = values.token
        ? { token: values.token, ca_cert: values.ca_cert || undefined }
        : undefined;
    } else {
      credentials = undefined;
    }

    const body: ClusterWriteInput = {
      display_name: values.display_name,
      k8s_auth_kind: values.k8s_auth_kind,
      k8s_api_url: values.k8s_api_url || undefined,
      credentials,
      prometheus_url: values.prometheus_url,
      alertmanager_url: values.alertmanager_url,
      grafana_url: values.grafana_url || undefined,
      rules_namespace: values.rules_namespace,
      heartbeat_enabled: values.heartbeat_enabled,
      heartbeat_alertname: values.heartbeat_alertname,
      heartbeat_timeout_seconds: values.heartbeat_timeout_seconds,
      heartbeat_team_id: values.heartbeat_team_id ?? null,
    };
    if (!editing) {
      body.name = values.name;
    }
    return body;
  };

  const createMutation = useMutation({
    mutationFn: (values: ClusterFormValues) => createCluster(buildBody(values)),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      setDrawerOpen(false);
      setSecretReveal({
        webhook_token: result.webhook_token,
        am_config_snippet: result.am_config_snippet,
      });
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : "클러스터 생성에 실패했습니다");
    },
  });

  const updateMutation = useMutation({
    mutationFn: (values: ClusterFormValues) => {
      if (!editing) {
        return Promise.reject(new Error("no cluster selected"));
      }
      return updateCluster(editing.id, buildBody(values));
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      setDrawerOpen(false);
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : "클러스터 수정에 실패했습니다");
    },
  });

  const enabledMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      updateCluster(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["clusters"] }),
    onError: (err) => {
      message.error(err instanceof ApiError ? `변경 실패: ${err.detail}` : "변경에 실패했습니다");
    },
  });

  const rotateMutation = useMutation({
    mutationFn: (id: number) => updateCluster(id, { rotate_webhook_token: true }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
      if (result.webhook_token && result.am_config_snippet) {
        setSecretReveal({
          webhook_token: result.webhook_token,
          am_config_snippet: result.am_config_snippet,
        });
      }
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `토큰 회전 실패: ${err.detail}` : "토큰 회전에 실패했습니다",
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteCluster(id),
    onSuccess: () => {
      message.success("클러스터가 삭제되었습니다");
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제 실패: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  const copySecret = async () => {
    if (!secretReveal) return;
    const text = `${secretReveal.webhook_token}\n\n${secretReveal.am_config_snippet}`;
    try {
      await navigator.clipboard.writeText(text);
      message.success("클립보드에 복사되었습니다");
    } catch {
      message.error("복사에 실패했습니다");
    }
  };

  const columns = [
    { title: "이름", dataIndex: "name", key: "name" },
    { title: "표시 이름", dataIndex: "display_name", key: "display_name" },
    {
      title: "활성",
      dataIndex: "enabled",
      key: "enabled",
      render: (value: boolean, record: Cluster) => (
        <Switch
          checked={value}
          loading={enabledMutation.isPending && enabledMutation.variables?.id === record.id}
          onChange={(checked) => enabledMutation.mutate({ id: record.id, enabled: checked })}
        />
      ),
    },
    {
      title: "헬스",
      key: "health",
      render: (_: unknown, record: Cluster) => {
        const health = healthByClusterId.get(record.id);
        return (
          <Space size={6}>
            <ComponentDot label="k8s" health={health?.k8s} />
            <ComponentDot label="Prometheus" health={health?.prometheus} />
            <ComponentDot label="Alertmanager" health={health?.alertmanager} />
          </Space>
        );
      },
    },
    {
      title: "하트비트",
      key: "heartbeat",
      render: (_: unknown, record: Cluster) => (
        <Space size={6}>
          <Badge
            color={HEARTBEAT_STATE_COLOR[record.heartbeat_state] ?? "default"}
            text={record.heartbeat_state}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.last_heartbeat_at
              ? `마지막 수신 ${dayjs(record.last_heartbeat_at).fromNow()}`
              : "수신 이력 없음"}
          </Text>
        </Space>
      ),
    },
    {
      title: "",
      key: "actions",
      width: 280,
      render: (_: unknown, record: Cluster) => (
        <Space size={8}>
          <Button size="small" onClick={() => openEdit(record)}>
            수정
          </Button>
          <Popconfirm
            title="웹훅 토큰을 회전하시겠습니까?"
            description="기존 토큰은 즉시 무효화됩니다."
            onConfirm={() => rotateMutation.mutate(record.id)}
          >
            <Button
              size="small"
              loading={rotateMutation.isPending && rotateMutation.variables === record.id}
            >
              토큰 회전
            </Button>
          </Popconfirm>
          <Popconfirm
            title="이 클러스터를 삭제하시겠습니까?"
            onConfirm={() => deleteMutation.mutate(record.id)}
          >
            <Button
              size="small"
              danger
              loading={deleteMutation.isPending && deleteMutation.variables === record.id}
            >
              삭제
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" onClick={openCreate} style={{ marginBottom: 16 }}>
        클러스터 추가
      </Button>

      <Table<Cluster>
        rowKey="id"
        loading={clustersQuery.isLoading}
        dataSource={clusters}
        columns={columns}
        pagination={false}
      />

      <Drawer
        title={editing ? `클러스터 수정 — ${editing.name}` : "클러스터 추가"}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={520}
        extra={
          <Button
            type="primary"
            loading={createMutation.isPending || updateMutation.isPending}
            onClick={() => form.submit()}
          >
            저장
          </Button>
        }
      >
        {formError && (
          <Alert type="error" showIcon message={formError} style={{ marginBottom: 16 }} />
        )}
        <Form<ClusterFormValues>
          form={form}
          layout="vertical"
          onFinish={(values) =>
            editing ? updateMutation.mutate(values) : createMutation.mutate(values)
          }
        >
          <Form.Item
            name="name"
            label="이름 (slug)"
            help={editing ? "생성 후 변경할 수 없습니다" : "예: staging"}
            rules={[
              { required: true, message: "이름을 입력하세요" },
              { pattern: NAME_PATTERN, message: "소문자/숫자/하이픈만 사용할 수 있습니다" },
            ]}
          >
            <Input disabled={!!editing} />
          </Form.Item>
          <Form.Item
            name="display_name"
            label="표시 이름"
            rules={[{ required: true, message: "표시 이름을 입력하세요" }]}
          >
            <Input />
          </Form.Item>

          <Form.Item name="k8s_auth_kind" label="k8s 인증 방식" rules={[{ required: true }]}>
            <Radio.Group options={AUTH_KIND_OPTIONS} optionType="button" />
          </Form.Item>

          {authKind === "kubeconfig" && (
            <Form.Item
              name="kubeconfig_yaml"
              label="kubeconfig"
              help={
                editing
                  ? "비워두면 기존 자격증명을 유지합니다"
                  : "비워두면 백엔드 호스트의 기본 kubeconfig를 사용합니다"
              }
            >
              <Input.TextArea
                rows={5}
                style={{ fontFamily: "monospace" }}
                placeholder="apiVersion: v1..."
              />
            </Form.Item>
          )}

          {authKind === "token" && (
            <>
              <Form.Item
                name="k8s_api_url"
                label="k8s API URL"
                rules={[{ required: true, message: "API URL을 입력하세요" }]}
              >
                <Input placeholder="https://cluster.example.com:6443" />
              </Form.Item>
              <Form.Item
                name="token"
                label="토큰"
                help={editing ? "비워두면 기존 토큰을 유지합니다" : undefined}
              >
                <Input.Password />
              </Form.Item>
              <Form.Item name="ca_cert" label="CA 인증서 (선택)">
                <Input.TextArea rows={3} style={{ fontFamily: "monospace" }} />
              </Form.Item>
            </>
          )}

          <Form.Item
            name="prometheus_url"
            label="Prometheus URL"
            rules={[{ required: true, message: "Prometheus URL을 입력하세요" }]}
          >
            <Input placeholder="http://localhost:30090" />
          </Form.Item>
          <Form.Item
            name="alertmanager_url"
            label="Alertmanager URL"
            rules={[{ required: true, message: "Alertmanager URL을 입력하세요" }]}
          >
            <Input placeholder="http://localhost:30093" />
          </Form.Item>
          <Form.Item name="grafana_url" label="Grafana URL (선택)">
            <Input placeholder="https://grafana.example.com" />
          </Form.Item>
          <Form.Item
            name="rules_namespace"
            label="룰 네임스페이스"
            rules={[{ required: true, message: "네임스페이스를 입력하세요" }]}
          >
            <Input placeholder="kam-rules" />
          </Form.Item>

          <Divider>하트비트 (Deadman's switch)</Divider>
          <Form.Item name="heartbeat_enabled" label="하트비트 사용" valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="heartbeat_alertname" label="하트비트 알럿명">
            <Input placeholder="Watchdog" />
          </Form.Item>
          <Form.Item name="heartbeat_timeout_seconds" label="타임아웃 (초)">
            <InputNumber style={{ width: "100%" }} min={1} />
          </Form.Item>
          <Form.Item name="heartbeat_team_id" label="귀속 팀">
            <Select
              allowClear
              placeholder="선택 안 함"
              options={(teamsQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
            />
          </Form.Item>
        </Form>
      </Drawer>

      <Modal
        title="웹훅 토큰"
        open={!!secretReveal}
        onCancel={() => setSecretReveal(null)}
        footer={[
          <Button key="copy" onClick={copySecret}>
            복사
          </Button>,
          <Button key="close" type="primary" onClick={() => setSecretReveal(null)}>
            닫기
          </Button>,
        ]}
      >
        <Alert
          type="warning"
          showIcon
          message="이 토큰은 지금만 표시됩니다. 다시 볼 수 없으니 반드시 지금 저장하세요."
          style={{ marginBottom: 16 }}
        />
        <Text strong>웹훅 토큰</Text>
        <pre
          style={{
            background: "#f5f5f5",
            padding: 12,
            borderRadius: 4,
            wordBreak: "break-all",
            whiteSpace: "pre-wrap",
            fontSize: 12,
          }}
        >
          {secretReveal?.webhook_token}
        </pre>
        <Text strong>Alertmanager 설정 스니펫</Text>
        <pre
          style={{
            background: "#f5f5f5",
            padding: 12,
            borderRadius: 4,
            whiteSpace: "pre-wrap",
            fontSize: 12,
          }}
        >
          {secretReveal?.am_config_snippet}
        </pre>
      </Modal>
    </div>
  );
}
