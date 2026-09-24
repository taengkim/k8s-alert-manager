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
import { useI18n } from "../i18n";

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
  const { t } = useI18n();
  if (!health) {
    return (
      <Tooltip title={`${label}: ${t("adminClusters.healthLabelUnknown")}`}>
        <Badge status="default" />
      </Tooltip>
    );
  }
  const title = health.ok
    ? `${label}: ${t("adminClusters.healthLabelOk")}${health.latency_ms != null ? ` (${health.latency_ms}ms)` : ""}`
    : `${label}: ${t("adminClusters.healthLabelFail")}${health.error ? ` — ${health.error}` : ""}`;
  return (
    <Tooltip title={title}>
      <Badge status={health.ok ? "success" : "error"} />
    </Tooltip>
  );
}

export default function AdminClusters() {
  const { t } = useI18n();
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
      setFormError(err instanceof ApiError ? err.detail : t("adminClusters.createError"));
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
      setFormError(err instanceof ApiError ? err.detail : t("adminClusters.updateError"));
    },
  });

  const enabledMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      updateCluster(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["clusters"] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.updateError")}: ${err.detail}` : t("common.updateError"),
      );
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
        err instanceof ApiError ? `${t("adminClusters.rotateError")}: ${err.detail}` : t("adminClusters.rotateError"),
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteCluster(id),
    onSuccess: () => {
      message.success(t("adminClusters.deleteSuccess"));
      queryClient.invalidateQueries({ queryKey: ["clusters"] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  const copySecret = async () => {
    if (!secretReveal) return;
    const text = `${secretReveal.webhook_token}\n\n${secretReveal.am_config_snippet}`;
    try {
      await navigator.clipboard.writeText(text);
      message.success(t("common.copySuccess"));
    } catch {
      message.error(t("adminClusters.copyError"));
    }
  };

  const columns = [
    { title: t("common.name"), dataIndex: "name", key: "name" },
    { title: t("adminClusters.displayNameColumn"), dataIndex: "display_name", key: "display_name" },
    {
      title: t("common.active"),
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
      title: t("adminClusters.healthColumn"),
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
      title: t("adminClusters.heartbeatColumn"),
      key: "heartbeat",
      render: (_: unknown, record: Cluster) => (
        <Space size={6}>
          <Badge
            color={HEARTBEAT_STATE_COLOR[record.heartbeat_state] ?? "default"}
            text={record.heartbeat_state}
          />
          <Text type="secondary" style={{ fontSize: 12 }}>
            {record.last_heartbeat_at
              ? t("adminClusters.lastHeartbeatReceived", { time: dayjs(record.last_heartbeat_at).fromNow() })
              : t("adminClusters.noHeartbeatHistory")}
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
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("adminClusters.rotateConfirmTitle")}
            description={t("adminClusters.rotateConfirmDesc")}
            onConfirm={() => rotateMutation.mutate(record.id)}
          >
            <Button
              size="small"
              loading={rotateMutation.isPending && rotateMutation.variables === record.id}
            >
              {t("adminClusters.rotateButton")}
            </Button>
          </Popconfirm>
          <Popconfirm
            title={t("adminClusters.deleteConfirm")}
            onConfirm={() => deleteMutation.mutate(record.id)}
          >
            <Button
              size="small"
              danger
              loading={deleteMutation.isPending && deleteMutation.variables === record.id}
            >
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" onClick={openCreate} style={{ marginBottom: 16 }}>
        {t("adminClusters.addButton")}
      </Button>

      <Table<Cluster>
        rowKey="id"
        loading={clustersQuery.isLoading}
        dataSource={clusters}
        columns={columns}
        pagination={false}
      />

      <Drawer
        title={editing ? t("adminClusters.editTitleWithName", { name: editing.name }) : t("adminClusters.addButton")}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        width={520}
        extra={
          <Button
            type="primary"
            loading={createMutation.isPending || updateMutation.isPending}
            onClick={() => form.submit()}
          >
            {t("common.save")}
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
            label={t("adminClusters.nameSlugLabel")}
            help={editing ? t("adminClusters.nameLockedHelp") : t("adminClusters.nameExampleHelp")}
            rules={[
              { required: true, message: t("common.nameRequired") },
              { pattern: NAME_PATTERN, message: t("ruleEditor.slugPattern") },
            ]}
          >
            <Input disabled={!!editing} />
          </Form.Item>
          <Form.Item
            name="display_name"
            label={t("adminClusters.displayNameColumn")}
            rules={[{ required: true, message: t("adminClusters.displayNameRequired") }]}
          >
            <Input />
          </Form.Item>

          <Form.Item name="k8s_auth_kind" label={t("adminClusters.authKindLabel")} rules={[{ required: true }]}>
            <Radio.Group options={AUTH_KIND_OPTIONS} optionType="button" />
          </Form.Item>

          {authKind === "kubeconfig" && (
            <Form.Item
              name="kubeconfig_yaml"
              label="kubeconfig"
              help={
                editing
                  ? t("adminClusters.kubeconfigHelpEdit")
                  : t("adminClusters.kubeconfigHelpCreate")
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
                rules={[{ required: true, message: t("adminClusters.apiUrlRequired") }]}
              >
                <Input placeholder="https://cluster.example.com:6443" />
              </Form.Item>
              <Form.Item
                name="token"
                label={t("adminClusters.tokenLabel")}
                help={editing ? t("adminClusters.tokenHelpEdit") : undefined}
              >
                <Input.Password />
              </Form.Item>
              <Form.Item name="ca_cert" label={t("adminClusters.caCertLabel")}>
                <Input.TextArea rows={3} style={{ fontFamily: "monospace" }} />
              </Form.Item>
            </>
          )}

          <Form.Item
            name="prometheus_url"
            label="Prometheus URL"
            rules={[{ required: true, message: t("adminClusters.prometheusUrlRequired") }]}
          >
            <Input placeholder="http://localhost:30090" />
          </Form.Item>
          <Form.Item
            name="alertmanager_url"
            label="Alertmanager URL"
            rules={[{ required: true, message: t("adminClusters.alertmanagerUrlRequired") }]}
          >
            <Input placeholder="http://localhost:30093" />
          </Form.Item>
          <Form.Item name="grafana_url" label={t("adminClusters.grafanaUrlOptionalLabel")}>
            <Input placeholder="https://grafana.example.com" />
          </Form.Item>
          <Form.Item
            name="rules_namespace"
            label={t("adminClusters.rulesNamespaceLabel")}
            rules={[{ required: true, message: t("adminClusters.namespaceRequired") }]}
          >
            <Input placeholder="kam-rules" />
          </Form.Item>

          <Divider>{t("adminClusters.heartbeatDivider")}</Divider>
          <Form.Item name="heartbeat_enabled" label={t("adminClusters.heartbeatEnabledLabel")} valuePropName="checked">
            <Switch />
          </Form.Item>
          <Form.Item name="heartbeat_alertname" label={t("adminClusters.heartbeatAlertNameLabel")}>
            <Input placeholder="Watchdog" />
          </Form.Item>
          <Form.Item name="heartbeat_timeout_seconds" label={t("adminClusters.timeoutSecondsLabel")}>
            <InputNumber style={{ width: "100%" }} min={1} />
          </Form.Item>
          <Form.Item
            name="heartbeat_team_id"
            label={t("adminClusters.owningTeamLabel")}
            help={t("adminClusters.owningTeamHelp")}
          >
            <Select
              allowClear
              placeholder={t("adminClusters.noneSelectedPlaceholder")}
              options={(teamsQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
            />
          </Form.Item>
        </Form>
      </Drawer>

      <Modal
        title={t("adminClusters.webhookTokenTitle")}
        open={!!secretReveal}
        onCancel={() => setSecretReveal(null)}
        footer={[
          <Button key="copy" onClick={copySecret}>
            {t("common.copy")}
          </Button>,
          <Button key="close" type="primary" onClick={() => setSecretReveal(null)}>
            {t("common.close")}
          </Button>,
        ]}
      >
        <Alert
          type="warning"
          showIcon
          message={t("adminClusters.tokenRevealWarning")}
          style={{ marginBottom: 16 }}
        />
        <Text strong>{t("adminClusters.webhookTokenTitle")}</Text>
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
        <Text strong>{t("adminClusters.amConfigSnippetLabel")}</Text>
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
