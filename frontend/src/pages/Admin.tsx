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

const { Title, Text } = Typography;

export default function Admin() {
  const { user } = useAuth();

  if (!user?.is_admin) {
    return (
      <div>
        <h2>관리자</h2>
        <Alert type="error" showIcon message="접근 권한이 없습니다 (403)" />
      </div>
    );
  }

  return (
    <div>
      <h2>관리자</h2>
      <Tabs
        items={[
          { key: "teams", label: "팀", children: <TeamsTab /> },
          { key: "users", label: "사용자", children: <UsersTab /> },
          { key: "clusters", label: "클러스터", children: <AdminClusters /> },
          { key: "settings", label: "설정", children: <SettingsTab /> },
          { key: "audit", label: "감사 로그", children: <AuditLog /> },
        ]}
      />
    </div>
  );
}

const SLUG_PATTERN = /^[a-z0-9][a-z0-9-]{1,62}$/;
const SLUG_HELP = "소문자, 숫자, 하이픈만 사용 (예: platform-team)";

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
      setFormError(err instanceof ApiError ? err.detail : "팀 생성에 실패했습니다");
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
      setFormError(err instanceof ApiError ? err.detail : "팀 수정에 실패했습니다");
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (teamId: number) => deleteTeam(teamId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["teams"] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  const columns = [
    { title: "슬러그", dataIndex: "slug", key: "slug" },
    { title: "이름", dataIndex: "name", key: "name" },
    { title: "설명", dataIndex: "description", key: "description" },
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
            수정
          </Button>
          <Popconfirm
            title="이 팀을 삭제하시겠습니까?"
            onConfirm={() => deleteMutation.mutate(record.id)}
          >
            <Button danger size="small">
              삭제
            </Button>
          </Popconfirm>
        </>
      ),
    },
  ];

  return (
    <div>
      <Button type="primary" onClick={() => setCreateOpen(true)} style={{ marginBottom: 16 }}>
        팀 생성
      </Button>
      <Table<Team>
        rowKey="id"
        loading={teamsQuery.isLoading}
        dataSource={teamsQuery.data ?? []}
        columns={columns}
        pagination={false}
      />

      <Modal
        title="팀 생성"
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
            label="슬러그"
            help={SLUG_HELP}
            rules={[{ required: true, pattern: SLUG_PATTERN, message: SLUG_HELP }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="name"
            label="이름"
            rules={[{ required: true, message: "이름을 입력하세요" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="description" label="설명">
            <Input.TextArea rows={2} />
          </Form.Item>
        </Form>
      </Modal>

      <Modal
        title="팀 수정"
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
            label="이름"
            rules={[{ required: true, message: "이름을 입력하세요" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="description" label="설명">
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
  const queryClient = useQueryClient();
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
    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(["admin-users"], context.previous);
      }
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ["admin-users"] });
    },
  });

  const columns = [
    { title: "아이디", dataIndex: "username", key: "username" },
    { title: "이름", dataIndex: "display_name", key: "display_name" },
    { title: "이메일", dataIndex: "email", key: "email" },
    {
      title: "관리자",
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
      title: "활성",
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

const RETENTION_FIELD_LABEL: Record<string, string> = {
  "retention.alert_events_days": "해소된 알럿 보관 기간 (일)",
  "retention.test_alert_events_days": "테스트 알럿 보관 기간 (일)",
  "retention.notification_outbox_days": "발송 이력 보관 기간 (일)",
  "retention.audit_log_days": "감사 로그 보관 기간 (일)",
  "retention.scheduled_actions_days": "예약 작업 이력 보관 기간 (일)",
};

const RETENTION_FIELD_HELP: Record<string, string> = {
  "retention.alert_events_days": "resolved 상태 알럿만 대상이며 firing 상태는 삭제되지 않습니다 (last_received_at 기준).",
  "retention.test_alert_events_days": "테스트 알럿(is_test)은 firing/resolved 상태와 무관하게 이 기간이 지나면 삭제됩니다.",
  "retention.notification_outbox_days": "발송 완료(delivered) 또는 포기(dead) 상태인 발송 이력만 대상입니다.",
  "retention.audit_log_days": "",
  "retention.scheduled_actions_days": "완료(done) 또는 취소(cancelled)된 에스컬레이션/재알림 예약만 대상입니다.",
};

const RETENTION_SUMMARY_LABEL: Record<keyof RetentionPurgeSummary, string> = {
  alert_events: "알럿 이벤트",
  notification_outbox: "발송 이력",
  scheduled_actions: "예약 작업",
  audit_logs: "감사 로그",
};

function SettingsTab() {
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
      message.success("설정이 저장되었습니다");
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : "설정 저장에 실패했습니다");
    },
  });

  const purgeMutation = useMutation({
    mutationFn: runRetentionPurge,
    onSuccess: ({ summary }) => {
      setPurgeSummary(summary);
      message.success("정리가 완료되었습니다");
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `정리 실행에 실패했습니다: ${err.detail}` : "정리 실행에 실패했습니다",
      );
    },
  });

  const keys = Object.keys(settingsQuery.data ?? RETENTION_FIELD_LABEL);

  return (
    <Space direction="vertical" size="large" style={{ width: "100%", maxWidth: 600 }}>
      <Card title="데이터 보관 정책 (Retention)">
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => saveMutation.mutate(values)}
        >
          {keys.map((key) => (
            <Form.Item
              key={key}
              name={key}
              label={RETENTION_FIELD_LABEL[key] ?? key}
              help={RETENTION_FIELD_HELP[key] || undefined}
              rules={[{ required: true, type: "number", min: 1, message: "1 이상의 정수를 입력하세요" }]}
            >
              <InputNumber min={1} style={{ width: 200 }} />
            </Form.Item>
          ))}
          <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>
            저장
          </Button>
        </Form>
      </Card>

      <Card title="지금 정리 실행">
        <Space direction="vertical">
          <Text type="secondary">
            위 보관 기간을 기준으로 즉시 오래된 데이터를 삭제합니다. 평소에는 매일 자동으로
            실행됩니다.
          </Text>
          <Popconfirm
            title="지금 데이터 정리를 실행하시겠습니까?"
            onConfirm={() => purgeMutation.mutate()}
          >
            <Button danger loading={purgeMutation.isPending}>
              지금 정리 실행
            </Button>
          </Popconfirm>
        </Space>

        {purgeSummary && (
          <>
            <Title level={5} style={{ marginTop: 16 }}>
              마지막 실행 결과
            </Title>
            <Descriptions column={1} size="small" bordered>
              {(Object.keys(purgeSummary) as (keyof RetentionPurgeSummary)[]).map((key) => (
                <Descriptions.Item key={key} label={RETENTION_SUMMARY_LABEL[key]}>
                  {purgeSummary[key]}건 삭제
                </Descriptions.Item>
              ))}
            </Descriptions>
          </>
        )}
      </Card>
    </Space>
  );
}
