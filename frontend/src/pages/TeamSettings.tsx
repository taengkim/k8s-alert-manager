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

const ROLE_OPTIONS: { value: TeamRole; label: string }[] = [
  { value: "owner", label: "Owner" },
  { value: "member", label: "Member" },
];

export default function TeamSettings() {
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
        <h2>팀 설정</h2>
        <Alert
          type="info"
          showIcon
          message="소속된 팀이 없습니다"
          description="관리자에게 팀 추가를 요청하세요."
        />
      </div>
    );
  }

  return (
    <div>
      <h2>팀 설정 — {currentTeam.name}</h2>
      <Tabs
        items={[
          {
            key: "members",
            label: "멤버",
            children: (
              <MembersTab teamId={currentTeam.id} isOwner={isOwner} isAdmin={!!user?.is_admin} />
            ),
          },
          {
            key: "mappings",
            label: "LDAP 매핑",
            children: <MappingsTab teamId={currentTeam.id} isOwner={isOwner} />,
          },
          {
            key: "reports",
            label: "리포트",
            children: <ReportsTab teamId={currentTeam.id} isOwner={isOwner} />,
          },
          // Owner-only: a team's audit trail is an owner-level concern (see
          // app.api.audit's scoping), so a plain member never sees this tab
          // at all, matching the 403 the API itself would return.
          ...(isOwner
            ? [
                {
                  key: "audit",
                  label: "감사 로그",
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
      setFormError(err instanceof ApiError ? err.detail : "멤버 추가에 실패했습니다");
    },
  });

  const removeMemberMutation = useMutation({
    mutationFn: (membershipId: number) => removeMember(teamId, membershipId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["team-members", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  const columns = [
    { title: "아이디", dataIndex: "username", key: "username" },
    { title: "이름", dataIndex: "display_name", key: "display_name" },
    {
      title: "역할",
      dataIndex: "role",
      key: "role",
      render: (role: TeamRole) => <Tag color={role === "owner" ? "gold" : "blue"}>{role}</Tag>,
    },
    {
      title: "출처",
      dataIndex: "origin",
      key: "origin",
      render: (origin: Member["origin"]) =>
        origin === "ldap" ? (
          <Tooltip title="LDAP 매핑으로 동기화됨">
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
                title="이 멤버를 제거하시겠습니까?"
                onConfirm={() => removeMemberMutation.mutate(record.membership_id)}
              >
                <Button danger size="small">
                  제거
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
          멤버 추가
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
        title="멤버 추가"
        open={modalOpen}
        onCancel={() => {
          setModalOpen(false);
          setFormError(null);
        }}
        onOk={() => form.submit()}
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
              label="사용자"
              rules={[{ required: true, message: "사용자를 선택하세요" }]}
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
              label="사용자 ID"
              help="사용자 ID(숫자)를 입력하세요"
              rules={[{ required: true, message: "사용자 ID를 입력하세요" }]}
            >
              <InputNumber style={{ width: "100%" }} min={1} />
            </Form.Item>
          )}
          <Form.Item name="role" label="역할" initialValue="member" rules={[{ required: true }]}>
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
      setFormError(err instanceof ApiError ? err.detail : "매핑 추가에 실패했습니다");
    },
  });

  const removeMappingMutation = useMutation({
    mutationFn: (mappingId: number) => removeMapping(teamId, mappingId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["team-mappings", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  const columns = [
    { title: "LDAP 그룹 DN", dataIndex: "ldap_group_dn", key: "ldap_group_dn" },
    {
      title: "역할",
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
                title="이 매핑을 삭제하시겠습니까?"
                onConfirm={() => removeMappingMutation.mutate(record.id)}
              >
                <Button danger size="small">
                  삭제
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
          매핑 추가
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
        title="LDAP 매핑 추가"
        open={modalOpen}
        onCancel={() => {
          setModalOpen(false);
          setFormError(null);
        }}
        onOk={() => form.submit()}
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
            label="LDAP 그룹 DN"
            rules={[{ required: true, message: "DN을 입력하세요" }]}
          >
            <Input placeholder="cn=team-a,ou=groups,dc=example,dc=com" />
          </Form.Item>
          <Form.Item name="role" label="역할" initialValue="member" rules={[{ required: true }]}>
            <Select options={ROLE_OPTIONS} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

// -- Reports tab (Phase 20) ------------------------------------------------------

const CADENCE_LABELS: Record<ReportCadence, string> = {
  daily: "매일",
  weekly: "매주",
  monthly: "매월",
};

const WEEKDAY_OPTIONS = [
  { value: 0, label: "월요일" },
  { value: 1, label: "화요일" },
  { value: 2, label: "수요일" },
  { value: 3, label: "목요일" },
  { value: 4, label: "금요일" },
  { value: 5, label: "토요일" },
  { value: 6, label: "일요일" },
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

function describeSchedule(schedule: ReportSchedule): string {
  const time = `${String(schedule.hour).padStart(2, "0")}:00`;
  if (schedule.cadence === "weekly") {
    const weekdayLabel = WEEKDAY_OPTIONS.find((w) => w.value === schedule.weekday)?.label ?? "";
    return `${CADENCE_LABELS.weekly} ${weekdayLabel} ${time} (${schedule.timezone})`;
  }
  if (schedule.cadence === "monthly") {
    return `${CADENCE_LABELS.monthly} 1일 ${time} (${schedule.timezone})`;
  }
  return `${CADENCE_LABELS.daily} ${time} (${schedule.timezone})`;
}

function LastStatusTag({ schedule }: { schedule: ReportSchedule }) {
  if (!schedule.last_status) {
    return <Tag>미실행</Tag>;
  }
  if (schedule.last_status.startsWith("error")) {
    return (
      <Tooltip title={schedule.last_status}>
        <Tag color="red">오류</Tag>
      </Tooltip>
    );
  }
  return (
    <Tooltip title={schedule.last_run_at ? new Date(schedule.last_run_at).toLocaleString() : undefined}>
      <Tag color="green">정상</Tag>
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
      setFormError(err instanceof ApiError ? err.detail : "리포트 스케줄 생성에 실패했습니다");
    },
  });

  const updateMutation = useMutation({
    mutationFn: (values: ReportFormValues) => updateReportSchedule(editing!.id, toInput(values)),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] });
      closeModal();
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : "리포트 스케줄 수정에 실패했습니다");
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      updateReportSchedule(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `변경에 실패했습니다: ${err.detail}` : "변경에 실패했습니다",
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteReportSchedule(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  const runNowMutation = useMutation({
    mutationFn: (id: number) => runReportNow(id),
    onSuccess: (result) => {
      message.success(`${result.queued_channels}개 채널로 리포트를 발송했습니다`);
      queryClient.invalidateQueries({ queryKey: ["report-schedules", teamId] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `발송에 실패했습니다: ${err.detail}` : "발송에 실패했습니다",
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
    { title: "이름", dataIndex: "name", key: "name" },
    {
      title: "일정",
      key: "schedule",
      render: (_: unknown, record: ReportSchedule) => describeSchedule(record),
    },
    {
      title: "채널 수",
      key: "channels",
      render: (_: unknown, record: ReportSchedule) => record.channel_ids.length,
    },
    {
      title: "활성화",
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
      title: "상태",
      key: "status",
      render: (_: unknown, record: ReportSchedule) => <LastStatusTag schedule={record} />,
    },
    {
      title: "",
      key: "actions",
      render: (_: unknown, record: ReportSchedule) => (
        <div style={{ display: "flex", gap: 8 }}>
          <Button size="small" onClick={() => setPreviewSchedule(record)}>
            미리보기
          </Button>
          {isOwner && (
            <>
              <Popconfirm
                title="지금 리포트를 발송하시겠습니까?"
                description="스케줄은 그대로 유지되고, 채널로 즉시 발송됩니다."
                onConfirm={() => runNowMutation.mutate(record.id)}
              >
                <Button
                  size="small"
                  loading={runNowMutation.isPending && runNowMutation.variables === record.id}
                >
                  지금 발송
                </Button>
              </Popconfirm>
              <Button size="small" onClick={() => openEditModal(record)}>
                수정
              </Button>
              <Popconfirm
                title="이 리포트 스케줄을 삭제하시겠습니까?"
                onConfirm={() => deleteMutation.mutate(record.id)}
              >
                <Button size="small" danger>
                  삭제
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
          리포트 스케줄 생성
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
        title={editing ? "리포트 스케줄 수정" : "리포트 스케줄 생성"}
        open={modalOpen}
        onCancel={closeModal}
        onOk={() => form.submit()}
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
            label="이름"
            rules={[{ required: true, message: "이름을 입력하세요" }]}
          >
            <Input placeholder="주간 알럿 리포트" />
          </Form.Item>

          <Form.Item name="cadence" label="주기" rules={[{ required: true }]}>
            <Radio.Group
              options={[
                { label: "매일", value: "daily" },
                { label: "매주", value: "weekly" },
                { label: "매월", value: "monthly" },
              ]}
              optionType="button"
            />
          </Form.Item>

          {cadence === "weekly" && (
            <Form.Item
              name="weekday"
              label="요일"
              rules={[{ required: true, message: "요일을 선택하세요" }]}
            >
              <Select options={WEEKDAY_OPTIONS} />
            </Form.Item>
          )}

          <Form.Item name="hour" label="시각" rules={[{ required: true }]}>
            <Select options={HOUR_OPTIONS} />
          </Form.Item>

          <Form.Item name="timezone" label="타임존" rules={[{ required: true }]}>
            <Select showSearch optionFilterProp="label" options={TIMEZONE_OPTIONS} />
          </Form.Item>

          <Form.Item
            name="channel_ids"
            label="채널"
            rules={[{ required: true, message: "채널을 하나 이상 선택하세요" }]}
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
            label="메시지 템플릿"
            help="비워두면 기본 리포트 템플릿을 사용합니다"
          >
            <Select
              allowClear
              loading={templatesQuery.isLoading}
              placeholder="기본 템플릿"
              options={reportTemplates.map((t) => ({ value: t.id, label: t.name }))}
            />
          </Form.Item>
        </Form>
      </Modal>

      <Drawer
        title={previewSchedule ? `미리보기 — ${previewSchedule.name}` : "미리보기"}
        open={previewSchedule !== null}
        onClose={() => setPreviewSchedule(null)}
        width={520}
      >
        {previewQuery.isLoading && <Typography.Text type="secondary">불러오는 중...</Typography.Text>}
        {previewQuery.isError && (
          <Alert
            type="error"
            showIcon
            message={
              previewQuery.error instanceof ApiError
                ? previewQuery.error.detail
                : "미리보기를 불러오지 못했습니다"
            }
          />
        )}
        {previewQuery.data && (
          <Space direction="vertical" style={{ width: "100%" }} size={16}>
            <div>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                제목
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
