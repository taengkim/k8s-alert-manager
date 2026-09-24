import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Form, Input, Modal, Popconfirm, Select, Switch, Table, Tag } from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import {
  createChannel,
  deleteChannel,
  listChannelTypes,
  listChannels,
  patchChannel,
  testChannel,
} from "../api/channels";
import type { Channel, ChannelType } from "../api/channels";
import { listTemplates } from "../api/templates";
import JsonSchemaForm from "../components/JsonSchemaForm";
import TemplatePreviewPopover from "../components/TemplatePreviewPopover";

export default function Channels() {
  const { user } = useAuth();
  const { currentTeam } = useTeam();

  const isOwner = useMemo(() => {
    if (!user || !currentTeam) return false;
    if (user.is_admin) return true;
    const membership = user.teams.find((t) => t.id === currentTeam.id);
    return membership?.role === "owner";
  }, [user, currentTeam]);

  if (!currentTeam) {
    return (
      <div>
        <h2>채널</h2>
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
      <h2>채널 — {currentTeam.name}</h2>
      <ChannelsTable teamId={currentTeam.id} isOwner={isOwner} />
    </div>
  );
}

interface ChannelFormValues {
  name: string;
  type: string;
  config: Record<string, unknown>;
  template_id?: number;
  allow_cross_team_escalation: boolean;
}

interface ChannelsTableProps {
  teamId: number;
  isOwner: boolean;
}

function ChannelsTable({ teamId, isOwner }: ChannelsTableProps) {
  const queryClient = useQueryClient();
  const { message } = App.useApp();
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<Channel | null>(null);
  const [selectedType, setSelectedType] = useState<ChannelType | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [form] = Form.useForm<ChannelFormValues>();

  const channelsQuery = useQuery({
    queryKey: ["channels", teamId],
    queryFn: () => listChannels(teamId),
  });

  const typesQuery = useQuery({
    queryKey: ["channel-types"],
    queryFn: listChannelTypes,
  });

  const templatesQuery = useQuery({
    queryKey: ["templates", teamId],
    queryFn: () => listTemplates(teamId),
  });
  const selectedTemplateId = Form.useWatch("template_id", form);
  const selectedTemplate = (templatesQuery.data ?? []).find((t) => t.id === selectedTemplateId);

  const closeModal = () => {
    setModalOpen(false);
    setEditing(null);
    setSelectedType(null);
    setFormError(null);
  };

  const createMutation = useMutation({
    mutationFn: (values: ChannelFormValues) =>
      createChannel(teamId, {
        name: values.name,
        type: values.type,
        config: values.config ?? {},
        template_id: values.template_id ?? null,
        allow_cross_team_escalation: values.allow_cross_team_escalation ?? false,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["channels", teamId] });
      closeModal();
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : "채널 생성에 실패했습니다");
    },
  });

  const updateMutation = useMutation({
    mutationFn: (values: ChannelFormValues) =>
      patchChannel(editing!.id, {
        name: values.name,
        config: values.config ?? {},
        template_id: values.template_id ?? null,
        allow_cross_team_escalation: values.allow_cross_team_escalation ?? false,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["channels", teamId] });
      closeModal();
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : "채널 수정에 실패했습니다");
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      patchChannel(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["channels", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `변경에 실패했습니다: ${err.detail}` : "변경에 실패했습니다",
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteChannel(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["channels", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  const testMutation = useMutation({
    mutationFn: (id: number) => testChannel(id),
    onSuccess: () => message.success("테스트 알림을 발송했습니다"),
    onError: (err) => {
      message.error(
        err instanceof ApiError
          ? `테스트 발송에 실패했습니다: ${err.detail}`
          : "테스트 발송에 실패했습니다",
      );
    },
  });

  const openCreateModal = () => {
    setEditing(null);
    setSelectedType(null);
    setFormError(null);
    setModalOpen(true);
  };

  const openEditModal = (channel: Channel) => {
    const type = (typesQuery.data ?? []).find((t) => t.type_name === channel.type) ?? null;
    setEditing(channel);
    setSelectedType(type);
    setFormError(null);
    setModalOpen(true);
  };

  const columns = [
    { title: "이름", dataIndex: "name", key: "name" },
    {
      title: "타입",
      dataIndex: "type",
      key: "type",
      render: (type: string) => <Tag color="blue">{type}</Tag>,
    },
    {
      title: "활성화",
      dataIndex: "enabled",
      key: "enabled",
      render: (enabled: boolean, record: Channel) => (
        <Switch
          checked={enabled}
          disabled={!isOwner}
          loading={toggleMutation.isPending && toggleMutation.variables?.id === record.id}
          onChange={(checked) => toggleMutation.mutate({ id: record.id, enabled: checked })}
        />
      ),
    },
    {
      title: "",
      key: "actions",
      render: (_: unknown, record: Channel) => (
        <div style={{ display: "flex", gap: 8 }}>
          <Button
            size="small"
            loading={testMutation.isPending && testMutation.variables === record.id}
            onClick={() => testMutation.mutate(record.id)}
          >
            테스트
          </Button>
          {isOwner && (
            <>
              <Button size="small" onClick={() => openEditModal(record)}>
                수정
              </Button>
              <Popconfirm
                title="이 채널을 삭제하시겠습니까?"
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

  const typeOptions = (typesQuery.data ?? []).map((t) => ({
    value: t.type_name,
    label: t.display_name,
  }));

  return (
    <div>
      {isOwner && (
        <Button type="primary" onClick={openCreateModal} style={{ marginBottom: 16 }}>
          채널 추가
        </Button>
      )}
      <Table<Channel>
        rowKey="id"
        loading={channelsQuery.isLoading}
        dataSource={channelsQuery.data ?? []}
        columns={columns}
        pagination={false}
      />
      <Modal
        title={editing ? "채널 수정" : "채널 추가"}
        open={modalOpen}
        onCancel={closeModal}
        onOk={() => form.submit()}
        confirmLoading={createMutation.isPending || updateMutation.isPending}
        destroyOnClose
      >
        {formError && (
          <Alert type="error" message={formError} showIcon style={{ marginBottom: 16 }} />
        )}
        <Form
          form={form}
          layout="vertical"
          initialValues={
            editing
              ? {
                  name: editing.name,
                  type: editing.type,
                  config: editing.config,
                  template_id: editing.template_id ?? undefined,
                  allow_cross_team_escalation: editing.allow_cross_team_escalation,
                }
              : { allow_cross_team_escalation: false }
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
            <Input />
          </Form.Item>
          <Form.Item
            name="type"
            label="타입"
            rules={[{ required: true, message: "타입을 선택하세요" }]}
          >
            <Select
              options={typeOptions}
              disabled={!!editing}
              loading={typesQuery.isLoading}
              onChange={(value: string) => {
                const type = (typesQuery.data ?? []).find((t) => t.type_name === value) ?? null;
                setSelectedType(type);
              }}
            />
          </Form.Item>
          {selectedType && (
            <JsonSchemaForm schema={selectedType.json_schema} namePrefix={["config"]} />
          )}
          <Form.Item
            name="template_id"
            label="메시지 템플릿"
            help="비워두면 채널 타입의 기본 템플릿(또는 앱 기본 템플릿)을 사용합니다"
          >
            <Select
              allowClear
              loading={templatesQuery.isLoading}
              placeholder="기본값 상속"
              options={(templatesQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
            />
          </Form.Item>
          <TemplatePreviewPopover template={selectedTemplate} />
          <Form.Item
            name="allow_cross_team_escalation"
            label="타팀 에스컬레이션 허용"
            valuePropName="checked"
            help="켜면 다른 팀의 라우팅 규칙이 이 채널을 에스컬레이션 대상으로 선택할 수 있습니다."
          >
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
