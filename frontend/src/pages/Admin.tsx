import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Form, Input, Modal, Popconfirm, Switch, Table, Tabs } from "antd";
import { useAuth } from "../auth/AuthProvider";
import { ApiError } from "../api/client";
import { createTeam, deleteTeam, listTeams, patchTeam } from "../api/teams";
import { listUsers, patchUser } from "../api/admin";
import type { AdminUser, Team } from "../api/types";

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
