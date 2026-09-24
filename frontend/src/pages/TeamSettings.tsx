import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
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
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import { listChannels } from "../api/channels";
import type { Channel } from "../api/channels";
import {
  createReportSchedule,
  deleteReportSchedule,
  listReportSchedules,
  previewReportSchedule,
  runReportNow,
  updateReportSchedule,
} from "../api/reports";
import type { ReportCadence, ReportSchedule, ReportScheduleWriteInput } from "../api/reports";
import {
  addMapping,
  addMember,
  listMappings,
  listMembers,
  removeMapping,
  removeMember,
} from "../api/teams";
import { listTemplates } from "../api/templates";
import { listUsers } from "../api/admin";
import type { LdapMapping, Member, TeamRole } from "../api/types";
import AuditLog from "./AuditLog";
import { useI18n } from "../i18n";
import type { TranslationKey } from "../i18n";

const ROLE_OPTIONS: { value: TeamRole; label: string }[] = [
  { value: "owner", label: "Owner" },
  { value: "member", label: "Member" },
];

export default function TeamSettings() {
  const { t } = useI18n();
  const { user } = useAuth();
  const { currentTeam, teams } = useTeam();

  const isOwner = useMemo(() => {
    if (!user || !currentTeam) return false;
    if (user.is_admin) return true;
    const membership = user.teams.find((t) => t.id === currentTeam.id);
    return membership?.role === "owner";
  }, [user, currentTeam]);

  if (teams.length === 0 || !currentTeam) {
    return (
      <div>
        <h2>{t("nav.team")}</h2>
        <Alert
          type="info"
          showIcon
          message={t("common.noTeamAssigned")}
          description={t("common.requestTeamAssignment")}
        />
      </div>
    );
  }

  return (
    <div>
      <h2>{t("team.titleWithName", { team: currentTeam.name })}</h2>
      <Tabs
        items={[
          {
            key: "members",
            label: t("team.membersTab"),
            children: (
              <MembersTab teamId={currentTeam.id} isOwner={isOwner} isAdmin={!!user?.is_admin} />
            ),
          },
          {
            key: "mappings",
            label: t("team.mappingsTab"),
            children: <MappingsTab teamId={currentTeam.id} isOwner={isOwner} />,
          },
          {
            key: "reports",
            label: t("team.reportsTab"),
            children: <ReportsTab teamId={currentTeam.id} isOwner={isOwner} />,
          },
          // Owner-only: a team's audit trail is an owner-level concern (see
          // app.api.audit's scoping), so a plain member never sees this tab
          // at all, matching the 403 the API itself would return.
          ...(isOwner
            ? [
                {
                  key: "audit",
                  label: t("team.auditTab"),
                  children: <AuditLog fixedTeamId={currentTeam.id} />,
                },
              ]
            : []),
        ]}
      />
    </div>
  );
}

interface MembersTabProps {
  teamId: number;
  isOwner: boolean;
  isAdmin: boolean;
}

function MembersTab({ teamId, isOwner, isAdmin }: MembersTabProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [form] = Form.useForm<{ user_id: number; role: TeamRole }>();

  const membersQuery = useQuery({
    queryKey: ["team-members", teamId],
    queryFn: () => listMembers(teamId),
  });

  const usersQuery = useQuery({
    queryKey: ["admin-users"],
    queryFn: listUsers,
    enabled: isAdmin && modalOpen,
  });

  const addMemberMutation = useMutation({
    mutationFn: (values: { user_id: number; role: TeamRole }) => addMember(teamId, values),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["team-members", teamId] });
      setModalOpen(false);
      form.resetFields();
      setFormError(null);
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("team.memberAddError"));
    },
  });

  const removeMemberMutation = useMutation({
    mutationFn: (membershipId: number) => removeMember(teamId, membershipId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["team-members", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  const columns = [
    { title: t("login.usernameLabel"), dataIndex: "username", key: "username" },
    { title: t("common.name"), dataIndex: "display_name", key: "display_name" },
    {
      title: t("team.roleColumn"),
      dataIndex: "role",
      key: "role",
      render: (role: TeamRole) => <Tag color={role === "owner" ? "gold" : "blue"}>{role}</Tag>,
    },
    {
      title: t("team.originColumn"),
      dataIndex: "origin",
      key: "origin",
      render: (origin: Member["origin"]) =>
        origin === "ldap" ? (
          <Tooltip title={t("team.ldapSyncedTooltip")}>
            <Tag color="purple">ldap</Tag>
          </Tooltip>
        ) : (
          <Tag>{origin}</Tag>
        ),
    },
    ...(isOwner
      ? [
          {
            title: "",
            key: "actions",
            render: (_: unknown, record: Member) => (
              <Popconfirm
                title={t("team.removeMemberConfirm")}
                onConfirm={() => removeMemberMutation.mutate(record.membership_id)}
              >
                <Button danger size="small">
                  {t("team.removeButton")}
                </Button>
              </Popconfirm>
            ),
          },
        ]
      : []),
  ];

  return (
    <div>
      {isOwner && (
        <Button type="primary" onClick={() => setModalOpen(true)} style={{ marginBottom: 16 }}>
          {t("team.addMemberButton")}
        </Button>
      )}
      <Table<Member>
        rowKey="membership_id"
        loading={membersQuery.isLoading}
        dataSource={membersQuery.data ?? []}
        columns={columns}
        pagination={false}
      />
      <Modal
        title={t("team.addMemberButton")}
        open={modalOpen}
        onCancel={() => {
          setModalOpen(false);
          setFormError(null);
        }}
        onOk={() => form.submit()}
        okText={t("common.save")}
        confirmLoading={addMemberMutation.isPending}
        destroyOnClose
      >
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form form={form} layout="vertical" onFinish={(values) => addMemberMutation.mutate(values)}>
          {isAdmin ? (
            <Form.Item
              name="user_id"
              label={t("team.userLabel")}
              rules={[{ required: true, message: t("team.userRequired") }]}
            >
              <Select
                loading={usersQuery.isLoading}
                showSearch
                optionFilterProp="label"
                options={(usersQuery.data ?? []).map((u) => ({
                  value: u.id,
                  label: `${u.username} (${u.display_name})`,
                }))}
              />
            </Form.Item>
          ) : (
            <Form.Item
              name="user_id"
              label={t("team.userIdLabel")}
              help={t("team.userIdHelp")}
              rules={[{ required: true, message: t("team.userIdRequired") }]}
            >
              <InputNumber style={{ width: "100%" }} min={1} />
            </Form.Item>
          )}
          <Form.Item name="role" label={t("team.roleColumn")} initialValue="member" rules={[{ required: true }]}>
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

interface MappingsTabProps {
  teamId: number;
  isOwner: boolean;
}

function MappingsTab({ teamId, isOwner }: MappingsTabProps) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [form] = Form.useForm<{ ldap_group_dn: string; role: TeamRole }>();

  const mappingsQuery = useQuery({
    queryKey: ["team-mappings", teamId],
    queryFn: () => listMappings(teamId),
  });

  const addMappingMutation = useMutation({
    mutationFn: (values: { ldap_group_dn: string; role: TeamRole }) => addMapping(teamId, values),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["team-mappings", teamId] });
      setModalOpen(false);
      form.resetFields();
      setFormError(null);
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("team.mappingAddError"));
    },
  });

  const removeMappingMutation = useMutation({
    mutationFn: (mappingId: number) => removeMapping(teamId, mappingId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["team-mappings", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  const columns = [
    { title: t("team.ldapGroupDnColumn"), dataIndex: "ldap_group_dn", key: "ldap_group_dn" },
    {
      title: t("team.roleColumn"),
      dataIndex: "role",
      key: "role",
      render: (role: TeamRole) => <Tag color={role === "owner" ? "gold" : "blue"}>{role}</Tag>,
    },
    ...(isOwner
      ? [
          {
            title: "",
            key: "actions",
            render: (_: unknown, record: LdapMapping) => (
              <Popconfirm
                title={t("team.deleteMappingConfirm")}
                onConfirm={() => removeMappingMutation.mutate(record.id)}
              >
                <Button danger size="small">
                  {t("common.delete")}
                </Button>
              </Popconfirm>
            ),
          },
        ]
      : []),
  ];

  return (
    <div>
      {isOwner && (
        <Button type="primary" onClick={() => setModalOpen(true)} style={{ marginBottom: 16 }}>
          {t("team.addMappingButton")}
        </Button>
      )}
      <Table<LdapMapping>
        rowKey="id"
        loading={mappingsQuery.isLoading}
        dataSource={mappingsQuery.data ?? []}
        columns={columns}
        pagination={false}
      />
      <Modal
        title={t("team.addMappingModalTitle")}
        open={modalOpen}
        onCancel={() => {
          setModalOpen(false);
          setFormError(null);
        }}
        onOk={() => form.submit()}
        okText={t("common.save")}
        confirmLoading={addMappingMutation.isPending}
        destroyOnClose
      >
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => addMappingMutation.mutate(values)}
        >
          <Form.Item
            name="ldap_group_dn"
            label={t("team.ldapGroupDnColumn")}
            rules={[{ required: true, message: t("team.dnRequired") }]}
          >
            <Input placeholder="cn=team-a,ou=groups,dc=example,dc=com" />
          </Form.Item>
          <Form.Item name="role" label={t("team.roleColumn")} initialValue="member" rules={[{ required: true }]}>
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

// -- Reports tab (Phase 20) ------------------------------------------------------

const CADENCE_LABEL_KEY: Record<ReportCadence, TranslationKey> = {
  daily: "team.cadenceDaily",
  weekly: "team.cadenceWeekly",
  monthly: "team.cadenceMonthly",
};

const WEEKDAY_LABEL_KEYS: TranslationKey[] = [
  "team.weekdayMon",
  "team.weekdayTue",
  "team.weekdayWed",
  "team.weekdayThu",
  "team.weekdayFri",
  "team.weekdaySat",
  "team.weekdaySun",
];

const HOUR_OPTIONS = Array.from({ length: 24 }, (_, hour) => ({
  value: hour,
  label: `${String(hour).padStart(2, "0")}:00`,
}));

// A curated list of commonly used IANA zones, searchable via antd Select's
// own filterOption -- not an exhaustive tz database dump (see this tab's
// brief: "주요 tz 목록 + 검색").
const TIMEZONE_OPTIONS = [
  "UTC",
  "Asia/Seoul",
  "Asia/Tokyo",
  "Asia/Shanghai",
  "Asia/Singapore",
  "Asia/Kolkata",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Australia/Sydney",
].map((tz) => ({ value: tz, label: tz }));

function describeSchedule(schedule: ReportSchedule, t: ReturnType<typeof useI18n>["t"]): string {
  const time = `${String(schedule.hour).padStart(2, "0")}:00`;
  if (schedule.cadence === "weekly") {
    const weekdayLabel = schedule.weekday != null ? t(WEEKDAY_LABEL_KEYS[schedule.weekday]) : "";
    return t("team.scheduleWeeklyDesc", {
      cadence: t(CADENCE_LABEL_KEY.weekly),
      weekday: weekdayLabel,
      time,
      timezone: schedule.timezone,
    });
  }
  if (schedule.cadence === "monthly") {
    return t("team.scheduleMonthlyDesc", {
      cadence: t(CADENCE_LABEL_KEY.monthly),
      time,
      timezone: schedule.timezone,
    });
  }
  return t("team.scheduleDailyDesc", {
    cadence: t(CADENCE_LABEL_KEY.daily),
    time,
    timezone: schedule.timezone,
  });
}

function LastStatusTag({ schedule }: { schedule: ReportSchedule }) {
  const { t } = useI18n();
  if (!schedule.last_status) {
    return <Tag>{t("team.notRunYet")}</Tag>;
  }
  if (schedule.last_status.startsWith("error")) {
    return (
      <Tooltip title={schedule.last_status}>
        <Tag color="red">{t("team.statusError")}</Tag>
      </Tooltip>
    );
  }
  return (
    <Tooltip title={schedule.last_run_at ? new Date(schedule.last_run_at).toLocaleString() : undefined}>
      <Tag color="green">{t("team.statusOk")}</Tag>
    </Tooltip>
  );
}

interface ReportsTabProps {
  teamId: number;
  isOwner: boolean;
}

interface ReportFormValues {
  name: string;
  cadence: ReportCadence;
  weekday?: number;
  hour: number;
  timezone: string;
  template_id?: number;
  channel_ids: number[];
}

function ReportsTab({ teamId, isOwner }: ReportsTabProps) {
  const { t } = useI18n();
  const WEEKDAY_OPTIONS = WEEKDAY_LABEL_KEYS.map((key, value) => ({ value, label: t(key) }));
  const CADENCE_OPTIONS = [
    { label: t("team.cadenceDaily"), value: "daily" },
    { label: t("team.cadenceWeekly"), value: "weekly" },
    { label: t("team.cadenceMonthly"), value: "monthly" },
  ];
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ReportSchedule | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [form] = Form.useForm<ReportFormValues>();
  const [previewSchedule, setPreviewSchedule] = useState<ReportSchedule | null>(null);
  const cadence = Form.useWatch("cadence", form);

  const schedulesQuery = useQuery({
    queryKey: ["report-schedules", teamId],
    queryFn: () => listReportSchedules(teamId),
  });

  const channelsQuery = useQuery({
    queryKey: ["channels", teamId],
    queryFn: () => listChannels(teamId),
  });

  const templatesQuery = useQuery({
    queryKey: ["templates", teamId],
    queryFn: () => listTemplates(teamId),
  });
  const reportTemplates = (templatesQuery.data ?? []).filter((t) => t.kind === "report");

  const previewQuery = useQuery({
    queryKey: ["report-preview", previewSchedule?.id],
    queryFn: () => previewReportSchedule(previewSchedule!.id),
    enabled: previewSchedule !== null,
  });

  const closeModal = () => {
    setModalOpen(false);
    setEditing(null);
    setFormError(null);
  };

  const toInput = (values: ReportFormValues): ReportScheduleWriteInput => ({
    name: values.name,
    cadence: values.cadence,
    weekday: values.cadence === "weekly" ? (values.weekday ?? 0) : null,
    hour: values.hour,
    timezone: values.timezone,
    template_id: values.template_id ?? null,
    channel_ids: values.channel_ids,
  });

  const createMutation = useMutation({
    mutationFn: (values: ReportFormValues) => createReportSchedule(teamId, toInput(values)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] });
      closeModal();
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("team.createScheduleError"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: (values: ReportFormValues) => updateReportSchedule(editing!.id, toInput(values)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] });
      closeModal();
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("team.updateScheduleError"));
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      updateReportSchedule(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.updateError")}: ${err.detail}` : t("common.updateError"),
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteReportSchedule(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  const runNowMutation = useMutation({
    mutationFn: (id: number) => runReportNow(id),
    onSuccess: (result) => {
      message.success(t("team.sendSuccess", { count: result.queued_channels }));
      queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("team.sendError")}: ${err.detail}` : t("team.sendError"),
      );
      queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] });
    },
  });

  const openCreateModal = () => {
    setEditing(null);
    setFormError(null);
    setModalOpen(true);
  };

  const openEditModal = (schedule: ReportSchedule) => {
    setEditing(schedule);
    setFormError(null);
    setModalOpen(true);
  };

  const columns = [
    { title: t("common.name"), dataIndex: "name", key: "name" },
    {
      title: t("team.scheduleColumn"),
      key: "schedule",
      render: (_: unknown, record: ReportSchedule) => describeSchedule(record, t),
    },
    {
      title: t("team.channelCountColumn"),
      key: "channels",
      render: (_: unknown, record: ReportSchedule) => record.channel_ids.length,
    },
    {
      title: t("common.enabled"),
      dataIndex: "enabled",
      key: "enabled",
      render: (enabled: boolean, record: ReportSchedule) => (
        <Switch
          checked={enabled}
          disabled={!isOwner}
          loading={toggleMutation.isPending && toggleMutation.variables?.id === record.id}
          onChange={(checked) => toggleMutation.mutate({ id: record.id, enabled: checked })}
        />
      ),
    },
    {
      title: t("common.status"),
      key: "status",
      render: (_: unknown, record: ReportSchedule) => <LastStatusTag schedule={record} />,
    },
    {
      title: "",
      key: "actions",
      render: (_: unknown, record: ReportSchedule) => (
        <div style={{ display: "flex", gap: 8 }}>
          <Button size="small" onClick={() => setPreviewSchedule(record)}>
            {t("ruleEditor.previewTitle")}
          </Button>
          {isOwner && (
            <>
              <Popconfirm
                title={t("team.runNowConfirmTitle")}
                description={t("team.runNowConfirmDesc")}
                onConfirm={() => runNowMutation.mutate(record.id)}
              >
                <Button
                  size="small"
                  loading={runNowMutation.isPending && runNowMutation.variables === record.id}
                >
                  {t("team.runNowButton")}
                </Button>
              </Popconfirm>
              <Button size="small" onClick={() => openEditModal(record)}>
                {t("common.edit")}
              </Button>
              <Popconfirm
                title={t("team.deleteScheduleConfirm")}
                onConfirm={() => deleteMutation.mutate(record.id)}
              >
                <Button size="small" danger>
                  {t("common.delete")}
                </Button>
              </Popconfirm>
            </>
          )}
        </div>
      ),
    },
  ];

  return (
    <div>
      {isOwner && (
        <Button type="primary" onClick={openCreateModal} style={{ marginBottom: 16 }}>
          {t("team.createScheduleButton")}
        </Button>
      )}
      <Table<ReportSchedule>
        rowKey="id"
        loading={schedulesQuery.isLoading}
        dataSource={schedulesQuery.data ?? []}
        columns={columns}
        pagination={false}
      />

      <Modal
        title={editing ? t("team.editScheduleTitle") : t("team.createScheduleButton")}
        open={modalOpen}
        onCancel={closeModal}
        onOk={() => form.submit()}
        okText={t("common.save")}
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnClose
        width={560}
      >
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form<ReportFormValues>
          form={form}
          layout="vertical"
          initialValues={
            editing
              ? {
                  name: editing.name,
                  cadence: editing.cadence,
                  weekday: editing.weekday ?? 0,
                  hour: editing.hour,
                  timezone: editing.timezone,
                  template_id: editing.template_id ?? undefined,
                  channel_ids: editing.channel_ids,
                }
              : { cadence: "weekly", weekday: 0, hour: 9, timezone: "UTC", channel_ids: [] }
          }
          onFinish={(values) =>
            editing ? updateMutation.mutate(values) : createMutation.mutate(values)
          }
        >
          <Form.Item
            name="name"
            label={t("common.name")}
            rules={[{ required: true, message: t("common.nameRequired") }]}
          >
            <Input placeholder={t("team.namePlaceholderExample")} />
          </Form.Item>

          <Form.Item name="cadence" label={t("team.cadenceLabel")} rules={[{ required: true }]}>
            <Radio.Group
              options={CADENCE_OPTIONS}
              optionType="button"
            />
          </Form.Item>

          {cadence === "weekly" && (
            <Form.Item
              name="weekday"
              label={t("team.weekdayLabel")}
              rules={[{ required: true, message: t("team.weekdayRequired") }]}
            >
              <Select options={WEEKDAY_OPTIONS} />
            </Form.Item>
          )}

          <Form.Item name="hour" label={t("team.hourLabel")} rules={[{ required: true }]}>
            <Select options={HOUR_OPTIONS} />
          </Form.Item>

          <Form.Item name="timezone" label={t("team.timezoneLabel")} rules={[{ required: true }]}>
            <Select showSearch optionFilterProp="label" options={TIMEZONE_OPTIONS} />
          </Form.Item>

          <Form.Item
            name="channel_ids"
            label={t("common.channel")}
            rules={[{ required: true, message: t("routeEditor.channelsRequired") }]}
          >
            <Select
              mode="multiple"
              loading={channelsQuery.isLoading}
              options={(channelsQuery.data ?? []).map((c: Channel) => ({
                value: c.id,
                label: `${c.name} (${c.type})`,
              }))}
            />
          </Form.Item>

          <Form.Item
            name="template_id"
            label={t("channels.messageTemplateLabel")}
            help={t("team.reportTemplateHelp")}
          >
            <Select
              allowClear
              loading={templatesQuery.isLoading}
              placeholder={t("team.defaultTemplatePlaceholder")}
              options={reportTemplates.map((t) => ({ value: t.id, label: t.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={previewSchedule ? t("team.previewTitleWithName", { name: previewSchedule.name }) : t("ruleEditor.previewTitle")}
        open={previewSchedule !== null}
        onClose={() => setPreviewSchedule(null)}
        width={520}
      >
        {previewQuery.isLoading && <Typography.Text type="secondary">{t("common.loading")}</Typography.Text>}
        {previewQuery.isError && (
          <Alert
            type="error"
            showIcon
            message={
              previewQuery.error instanceof ApiError
                ? previewQuery.error.detail
                : t("team.previewLoadError")
            }
          />
        )}
        {previewQuery.data && (
          <Space direction="vertical" style={{ width: "100%" }} size={16}>
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {t("templateEditor.renderedTitleLabel")}
              </Typography.Text>
              <div style={{ fontWeight: 600 }}>{previewQuery.data.title}</div>
            </div>
            {previewQuery.data.body_html ? (
              <iframe
                title="report-preview"
                sandbox=""
                srcDoc={previewQuery.data.body_html}
                style={{ width: "100%", height: 480, border: "1px solid #d9d9d9", borderRadius: 6 }}
              />
            ) : (
              <pre style={{ whiteSpace: "pre-wrap", margin: 0, fontFamily: "monospace" }}>
                {previewQuery.data.body}
              </pre>
            )}
          </Space>
        )}
      </Drawer>
    </div>
  );
}
