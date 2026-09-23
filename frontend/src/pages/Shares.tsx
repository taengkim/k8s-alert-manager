import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
  Empty,
  Form,
  Modal,
  Popconfirm,
  Radio,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Typography,
} from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import {
  createShare,
  deleteShare,
  listAllTeamsBrief,
  listIncomingShares,
  listOutgoingShares,
  updateShare,
} from "../api/shares";
import type {
  IncomingShare,
  OutgoingShare,
  ShareMatcher,
  ShareMode,
} from "../api/shares";
import MatcherListEditor from "../components/MatcherListEditor";

const { Text } = Typography;

const MODE_OPTIONS: { value: ShareMode; label: string }[] = [
  { value: "view", label: "보기만" },
  { value: "view_notify", label: "보기+알림" },
];

function modeLabel(mode: ShareMode): string {
  return mode === "view_notify" ? "보기+알림" : "보기만";
}

function modeColor(mode: ShareMode): string {
  return mode === "view_notify" ? "green" : "blue";
}

function matcherSummary(matchers: ShareMatcher[] | null): string {
  return !matchers || matchers.length === 0 ? "전체" : `매처 ${matchers.length}개`;
}

export default function Shares() {
  const { currentTeam, teams } = useTeam();

  if (teams.length === 0 || !currentTeam) {
    return (
      <div>
        <h2>공유</h2>
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
      <h2>공유 — {currentTeam.name}</h2>
      <Tabs
        items={[
          { key: "outgoing", label: "보내는 공유", children: <OutgoingTab teamId={currentTeam.id} /> },
          { key: "incoming", label: "받는 공유", children: <IncomingTab teamId={currentTeam.id} /> },
        ]}
      />
    </div>
  );
}

interface FormValues {
  target_team_id: number;
  mode: ShareMode;
  matchers: ShareMatcher[];
}

function OutgoingTab({ teamId }: { teamId: number }) {
  const { user } = useAuth();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const [form] = Form.useForm<FormValues>();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<OutgoingShare | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  const isOwner = useMemo(() => {
    if (!user) return false;
    if (user.is_admin) return true;
    return user.teams.find((t) => t.id === teamId)?.role === "owner";
  }, [user, teamId]);

  const sharesQuery = useQuery({
    queryKey: ["shares-outgoing", teamId],
    queryFn: () => listOutgoingShares(teamId),
  });
  const teamsQuery = useQuery({
    queryKey: ["teams-all-brief"],
    queryFn: listAllTeamsBrief,
    enabled: modalOpen,
  });

  const targetOptions = (teamsQuery.data ?? [])
    .filter((t) => t.id !== teamId)
    .map((t) => ({ value: t.id, label: `${t.name} (${t.slug})` }));

  const closeModal = () => {
    setModalOpen(false);
    setEditing(null);
    setFormError(null);
    form.resetFields();
  };

  const createMutation = useMutation({
    mutationFn: (values: FormValues) =>
      createShare(teamId, {
        target_team_id: values.target_team_id,
        mode: values.mode,
        matchers: values.matchers?.length ? values.matchers : undefined,
      }),
    onSuccess: () => {
      message.success("공유가 생성되었습니다");
      queryClient.invalidateQueries({ queryKey: ["shares-outgoing", teamId] });
      closeModal();
    },
    onError: (err) => setFormError(err instanceof ApiError ? err.detail : "공유 생성에 실패했습니다"),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, values }: { id: number; values: FormValues }) =>
      updateShare(id, {
        mode: values.mode,
        matchers: values.matchers?.length ? values.matchers : null,
      }),
    onSuccess: () => {
      message.success("공유가 수정되었습니다");
      queryClient.invalidateQueries({ queryKey: ["shares-outgoing", teamId] });
      closeModal();
    },
    onError: (err) => setFormError(err instanceof ApiError ? err.detail : "공유 수정에 실패했습니다"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteShare(id),
    onSuccess: () => {
      message.success("공유가 삭제되었습니다");
      queryClient.invalidateQueries({ queryKey: ["shares-outgoing", teamId] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    setModalOpen(true);
  };

  const openEdit = (share: OutgoingShare) => {
    setEditing(share);
    form.setFieldsValue({
      target_team_id: share.target_team_id,
      mode: share.mode,
      matchers: (share.matchers ?? []).map((m) => ({
        kind: m.kind,
        target: m.target,
        key: m.key ?? undefined,
        pattern: m.pattern,
      })),
    });
    setModalOpen(true);
  };

  const handleSubmit = (values: FormValues) => {
    setFormError(null);
    if (editing) {
      updateMutation.mutate({ id: editing.id, values });
    } else {
      createMutation.mutate(values);
    }
  };

  const columns = [
    {
      title: "대상 팀",
      key: "target",
      render: (_: unknown, s: OutgoingShare) => `${s.target_team_name} (${s.target_team_slug})`,
    },
    {
      title: "모드",
      dataIndex: "mode",
      key: "mode",
      render: (mode: ShareMode) => <Tag color={modeColor(mode)}>{modeLabel(mode)}</Tag>,
    },
    {
      title: "범위",
      key: "matchers",
      render: (_: unknown, s: OutgoingShare) => <Tag>{matcherSummary(s.matchers)}</Tag>,
    },
    ...(isOwner
      ? [
          {
            title: "",
            key: "actions",
            render: (_: unknown, s: OutgoingShare) => (
              <Space>
                <Button size="small" onClick={() => openEdit(s)}>
                  수정
                </Button>
                <Popconfirm
                  title="이 공유를 삭제하시겠습니까?"
                  onConfirm={() => deleteMutation.mutate(s.id)}
                >
                  <Button size="small" danger loading={deleteMutation.isPending}>
                    삭제
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]
      : []),
  ];

  return (
    <div>
      {isOwner && (
        <Button type="primary" onClick={openCreate} style={{ marginBottom: 16 }}>
          공유 생성
        </Button>
      )}
      <Table<OutgoingShare>
        rowKey="id"
        loading={sharesQuery.isLoading}
        dataSource={sharesQuery.data ?? []}
        columns={columns}
        pagination={false}
        locale={{ emptyText: <Empty description="공유가 없습니다" /> }}
      />
      <Modal
        title={editing ? "공유 수정" : "공유 생성"}
        open={modalOpen}
        onCancel={closeModal}
        onOk={() => form.submit()}
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnClose
        width={640}
      >
        {formError && <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />}
        <Form<FormValues>
          form={form}
          layout="vertical"
          onFinish={handleSubmit}
          initialValues={{ mode: "view", matchers: [] }}
        >
          <Form.Item
            name="target_team_id"
            label="대상 팀"
            rules={[{ required: true, message: "대상 팀을 선택하세요" }]}
          >
            <Select
              disabled={!!editing}
              showSearch
              optionFilterProp="label"
              loading={teamsQuery.isLoading}
              options={targetOptions}
              placeholder="공유할 팀 선택"
            />
          </Form.Item>
          <Form.Item name="mode" label="모드" rules={[{ required: true }]}>
            <Radio.Group options={MODE_OPTIONS} optionType="button" />
          </Form.Item>
          <Text type="secondary">
            보기만: 대상 팀의 대시보드/이력에만 표시됩니다. 보기+알림: 대상 팀의 include_shared
            라우팅 규칙도 이 알럿에 반응해 알림을 보낼 수 있습니다.
          </Text>
          <div style={{ marginTop: 16, marginBottom: 8 }}>
            <Text strong>공유 범위 (매처)</Text>
            <div>
              <Text type="secondary">비워두면 이 팀의 모든 알럿을 공유합니다.</Text>
            </div>
          </div>
          <MatcherListEditor name="matchers" form={form} />
        </Form>
      </Modal>
    </div>
  );
}

function IncomingTab({ teamId }: { teamId: number }) {
  const sharesQuery = useQuery({
    queryKey: ["shares-incoming", teamId],
    queryFn: () => listIncomingShares(teamId),
  });

  const columns = [
    {
      title: "보낸 팀",
      key: "owner",
      render: (_: unknown, s: IncomingShare) => `${s.owner_team_name} (${s.owner_team_slug})`,
    },
    {
      title: "모드",
      dataIndex: "mode",
      key: "mode",
      render: (mode: ShareMode) => <Tag color={modeColor(mode)}>{modeLabel(mode)}</Tag>,
    },
    {
      title: "범위",
      key: "matchers",
      render: (_: unknown, s: IncomingShare) => <Tag>{matcherSummary(s.matchers)}</Tag>,
    },
  ];

  return (
    <Table<IncomingShare>
      rowKey="id"
      loading={sharesQuery.isLoading}
      dataSource={sharesQuery.data ?? []}
      columns={columns}
      pagination={false}
      locale={{ emptyText: <Empty description="받은 공유가 없습니다" /> }}
    />
  );
}
