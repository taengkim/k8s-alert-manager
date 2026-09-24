import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tabs,
  Typography,
} from "antd";
import { useAuth } from "../auth/AuthProvider";
import { ApiError } from "../api/client";
import { createTeam, deleteTeam, listTeams, patchTeam } from "../api/teams";
import {
  getRetentionSettings,
  listUsers,
  patchUser,
  runRetentionPurge,
  updateRetentionSettings,
} from "../api/admin";
import type { RetentionPurgeSummary } from "../api/admin";
import type { AdminUser, Team } from "../api/types";
import AdminClusters from "./AdminClusters";
import AuditLog from "./AuditLog";
import { useI18n } from "../i18n";
import type { TranslationKey } from "../i18n";

const { Title, Text } = Typography;

export default function Admin() {
  const { t } = useI18n();
  const { user } = useAuth();

  if (!user?.is_admin) {
    return (
      <div>
        <h2>{t("nav.admin")}</h2>
        <Alert type="error" showIcon message={t("admin.accessDenied")} />
      </div>
    );
  }

  return (
    <div>
      <h2>{t("nav.admin")}</h2>
      <Tabs
        items={[
          { key: "teams", label: t("admin.teamsTab"), children: <TeamsTab /> },
          { key: "users", label: t("admin.usersTab"), children: <UsersTab /> },
          { key: "clusters", label: t("admin.clustersTab"), children: <AdminClusters /> },
          { key: "settings", label: t("audit.actionSettings"), children: <SettingsTab /> },
          { key: "audit", label: t("team.auditTab"), children: <AuditLog /> },
        ]}
      />
    </div>
  );
}

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{1,62}$/;

interface TeamFormValues {
  slug: string;
  name: string;
  description?: string;
}

interface TeamEditValues {
  name: string;
  description?: string;
}

function TeamsTab() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [createOpen, setCreateOpen] = useState(false);
  const [editTeam, setEditTeam] = useState<Team | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [createForm] = Form.useForm<TeamFormValues>();
  const [editForm] = Form.useForm<TeamEditValues>();

  const teamsQuery = useQuery({ queryKey: ["teams"], queryFn: listTeams });

  const createMutation = useMutation({
    mutationFn: (values: TeamFormValues) => createTeam(values),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["teams"] });
      setCreateOpen(false);
      createForm.resetFields();
      setFormError(null);
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("admin.teamCreateError"));
    },
  });

  const editMutation = useMutation({
    mutationFn: (values: TeamEditValues) => {
      if (!editTeam) {
        return Promise.reject(new Error("no team selected"));
      }
      return patchTeam(editTeam.id, values);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["teams"] });
      setEditTeam(null);
      setFormError(null);
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("admin.teamUpdateError"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (teamId: number) => deleteTeam(teamId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["teams"] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  const columns = [
    { title: t("rules.slugColumn"), dataIndex: "slug", key: "slug" },
    { title: t("common.name"), dataIndex: "name", key: "name" },
    { title: t("common.description"), dataIndex: "description", key: "description" },
    {
      title: "",
      key: "actions",
      render: (_: unknown, record: Team) => (
        <>
          <Button
            size="small"
            style={{ marginRight: 8 }}
            onClick={() => {
              setEditTeam(record);
              editForm.setFieldsValue({
                name: record.name,
                description: record.description ?? undefined,
              });
            }}
          >
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("admin.deleteTeamConfirm")}
            onConfirm={() => deleteMutation.mutate(record.id)}
          >
            <Button danger size="small">
              {t("common.delete")}
            </Button>
          </Popconfirm>
        </>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" onClick={() => setCreateOpen(true)} style={{ marginBottom: 16 }}>
        {t("admin.createTeamButton")}
      </Button>
      <Table<Team>
        rowKey="id"
        loading={teamsQuery.isLoading}
        dataSource={teamsQuery.data ?? []}
        columns={columns}
        pagination={false}
      />

      <Modal
        title={t("admin.createTeamButton")}
        open={createOpen}
        onCancel={() => {
          setCreateOpen(false);
          setFormError(null);
        }}
        onOk={() => createForm.submit()}
        confirmLoading={createMutation.isPending}
        destroyOnClose
      >
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form
          form={createForm}
          layout="vertical"
          onFinish={(values) => createMutation.mutate(values)}
        >
          <Form.Item
            name="slug"
            label={t("rules.slugColumn")}
            help={t("admin.slugHelp")}
            rules={[{ required: true, pattern: SLUG_PATTERN, message: t("admin.slugHelp") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="name"
            label={t("common.name")}
            rules={[{ required: true, message: t("common.nameRequired") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t("common.description")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title={t("admin.editTeamTitle")}
        open={!!editTeam}
        onCancel={() => {
          setEditTeam(null);
          setFormError(null);
        }}
        onOk={() => editForm.submit()}
        confirmLoading={editMutation.isPending}
        destroyOnClose
      >
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form form={editForm} layout="vertical" onFinish={(values) => editMutation.mutate(values)}>
          <Form.Item
            name="name"
            label={t("common.name")}
            rules={[{ required: true, message: t("common.nameRequired") }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="description" label={t("common.description")}>
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

interface PatchUserVars {
  id: number;
  body: { is_admin?: boolean; is_active?: boolean };
}

interface PatchUserContext {
  previous?: AdminUser[];
}

function UsersTab() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const usersQuery = useQuery({ queryKey: ["admin-users"], queryFn: listUsers });

  const patchMutation = useMutation<AdminUser, Error, PatchUserVars, PatchUserContext>({
    mutationFn: ({ id, body }) => patchUser(id, body),
    onMutate: async ({ id, body }) => {
      await queryClient.cancelQueries({ queryKey: ["admin-users"] });
      const previous = queryClient.getQueryData<AdminUser[]>(["admin-users"]);
      queryClient.setQueryData<AdminUser[]>(["admin-users"], (old) =>
        old?.map((u) => (u.id === id ? { ...u, ...body } : u)),
      );
      return { previous };
    },
    onError: (err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["admin-users"], context.previous);
      }
      message.error(
        err instanceof ApiError ? `${t("admin.userUpdateError")}: ${err.detail}` : t("admin.userUpdateError"),
      );
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });

  const columns = [
    { title: t("login.usernameLabel"), dataIndex: "username", key: "username" },
    { title: t("common.name"), dataIndex: "display_name", key: "display_name" },
    { title: t("admin.emailColumn"), dataIndex: "email", key: "email" },
    {
      title: t("admin.adminColumn"),
      dataIndex: "is_admin",
      key: "is_admin",
      render: (value: boolean, record: AdminUser) => (
        <Switch
          checked={value}
          onChange={(checked) => patchMutation.mutate({ id: record.id, body: { is_admin: checked } })}
        />
      ),
    },
    {
      title: t("common.active"),
      dataIndex: "is_active",
      key: "is_active",
      render: (value: boolean, record: AdminUser) => (
        <Switch
          checked={value}
          onChange={(checked) => patchMutation.mutate({ id: record.id, body: { is_active: checked } })}
        />
      ),
    },
  ];

  return (
    <Table<AdminUser>
      rowKey="id"
      loading={usersQuery.isLoading}
      dataSource={usersQuery.data ?? []}
      columns={columns}
      pagination={false}
    />
  );
}

// -- Phase 15: retention settings ----------------------------------------

const RETENTION_FIELD_LABEL_KEY: Record<string, TranslationKey> = {
  "retention.alert_events_days": "admin.retentionAlertEvents",
  "retention.test_alert_events_days": "admin.retentionTestAlerts",
  "retention.notification_outbox_days": "admin.retentionNotificationOutbox",
  "retention.audit_log_days": "admin.retentionAuditLog",
  "retention.scheduled_actions_days": "admin.retentionScheduledActions",
};

const RETENTION_FIELD_HELP_KEY: Record<string, TranslationKey | null> = {
  "retention.alert_events_days": "admin.retentionAlertEventsHelp",
  "retention.test_alert_events_days": "admin.retentionTestAlertsHelp",
  "retention.notification_outbox_days": "admin.retentionNotificationOutboxHelp",
  "retention.audit_log_days": null,
  "retention.scheduled_actions_days": "admin.retentionScheduledActionsHelp",
};

const RETENTION_SUMMARY_LABEL_KEY: Record<keyof RetentionPurgeSummary, TranslationKey> = {
  alert_events: "admin.summaryAlertEvents",
  notification_outbox: "admin.summaryNotificationOutbox",
  scheduled_actions: "admin.summaryScheduledActions",
  audit_logs: "admin.summaryAuditLogs",
};

function SettingsTab() {
  const { t } = useI18n();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<Record<string, number>>();
  const [formError, setFormError] = useState<string | null>(null);
  const [purgeSummary, setPurgeSummary] = useState<RetentionPurgeSummary | null>(null);

  const settingsQuery = useQuery({
    queryKey: ["admin-retention-settings"],
    queryFn: getRetentionSettings,
  });

  useEffect(() => {
    if (settingsQuery.data) {
      form.setFieldsValue(settingsQuery.data);
    }
  }, [settingsQuery.data, form]);

  const saveMutation = useMutation({
    mutationFn: (values: Record<string, number>) => updateRetentionSettings(values),
    onSuccess: (data) => {
      queryClient.setQueryData(["admin-retention-settings"], data);
      setFormError(null);
      message.success(t("admin.settingsSaveSuccess"));
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("admin.settingsSaveError"));
    },
  });

  const purgeMutation = useMutation({
    mutationFn: runRetentionPurge,
    onSuccess: ({ summary }) => {
      setPurgeSummary(summary);
      message.success(t("admin.purgeSuccess"));
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("admin.purgeError")}: ${err.detail}` : t("admin.purgeError"),
      );
    },
  });

  const keys = Object.keys(settingsQuery.data ?? RETENTION_FIELD_LABEL_KEY);

  return (
    <Space direction="vertical" size="large" style={{ width: "100%", maxWidth: 600 }}>
      <Card title={t("admin.retentionPolicyTitle")}>
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => saveMutation.mutate(values)}
        >
          {keys.map((key) => {
            const labelKey = RETENTION_FIELD_LABEL_KEY[key];
            const helpKey = RETENTION_FIELD_HELP_KEY[key];
            return (
              <Form.Item
                key={key}
                name={key}
                label={labelKey ? t(labelKey) : key}
                help={helpKey ? t(helpKey) : undefined}
                rules={[{ required: true, type: "number", min: 1, message: t("admin.minIntegerRequired") }]}
              >
                <InputNumber min={1} style={{ width: 200 }} />
              </Form.Item>
            );
          })}
          <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>
            {t("common.save")}
          </Button>
        </Form>
      </Card>

      <Card title={t("admin.runCleanupTitle")}>
        <Space direction="vertical">
          <Text type="secondary">{t("admin.cleanupDescription")}</Text>
          <Popconfirm
            title={t("admin.runCleanupConfirm")}
            onConfirm={() => purgeMutation.mutate()}
          >
            <Button danger loading={purgeMutation.isPending}>
              {t("admin.runCleanupTitle")}
            </Button>
          </Popconfirm>
        </Space>

        {purgeSummary && (
          <>
            <Title level={5} style={{ marginTop: 16 }}>
              {t("admin.lastRunResultTitle")}
            </Title>
            <Descriptions column={1} size="small" bordered>
              {(Object.keys(purgeSummary) as (keyof RetentionPurgeSummary)[]).map((key) => (
                <Descriptions.Item key={key} label={t(RETENTION_SUMMARY_LABEL_KEY[key])}>
                  {t("admin.deletedCount", { count: purgeSummary[key] })}
                </Descriptions.Item>
              ))}
            </Descriptions>
          </>
        )}
      </Card>
    </Space>
  );
}
