import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import {
  addMapping,
  addMember,
  listMappings,
  listMembers,
  removeMapping,
  removeMember,
} from "../api/teams";
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
