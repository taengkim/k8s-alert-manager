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
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
} from "antd";
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
import type { Channel, ChannelType, DigestMode } from "../api/channels";
import { listTemplates } from "../api/templates";
import JsonSchemaForm from "../components/JsonSchemaForm";
import TemplatePreviewPopover from "../components/TemplatePreviewPopover";
import { useI18n } from "../i18n";

export default function Channels() {
  const { t } = useI18n();
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
        <h2>{t("channels.title")}</h2>
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
      <h2>{t("channels.titleWithTeam", { team: currentTeam.name })}</h2>
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
  rate_limit_per_hour?: number;
  digest_mode: DigestMode;
  digest_window_minutes: number;
}

interface ChannelsTableProps {
  teamId: number;
  isOwner: boolean;
}

function ChannelsTable({ teamId, isOwner }: ChannelsTableProps) {
  const { t } = useI18n();
  const DIGEST_MODE_OPTIONS: { label: string; value: DigestMode }[] = [
    { label: t("channels.digestOff"), value: "off" },
    { label: t("channels.digestAuto"), value: "auto" },
    { label: t("channels.digestAlways"), value: "always" },
  ];
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
        rate_limit_per_hour: values.rate_limit_per_hour ?? null,
        digest_mode: values.digest_mode ?? "off",
        digest_window_minutes: values.digest_window_minutes ?? 5,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["channels", teamId] });
      closeModal();
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("channels.createError"));
    },
  });

  const updateMutation = useMutation({
    mutationFn: (values: ChannelFormValues) =>
      patchChannel(editing!.id, {
        name: values.name,
        config: values.config ?? {},
        template_id: values.template_id ?? null,
        allow_cross_team_escalation: values.allow_cross_team_escalation ?? false,
        rate_limit_per_hour: values.rate_limit_per_hour ?? null,
        digest_mode: values.digest_mode ?? "off",
        digest_window_minutes: values.digest_window_minutes ?? 5,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["channels", teamId] });
      closeModal();
    },
    onError: (err) => {
      setFormError(err instanceof ApiError ? err.detail : t("channels.updateError"));
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      patchChannel(id, { enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["channels", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.updateError")}: ${err.detail}` : t("common.updateError"),
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteChannel(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["channels", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  const testMutation = useMutation({
    mutationFn: (id: number) => testChannel(id),
    onSuccess: () => message.success(t("channels.testSuccess")),
    onError: (err) => {
      message.error(
        err instanceof ApiError
          ? `${t("channels.testError")}: ${err.detail}`
          : t("channels.testError"),
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
    { title: t("common.name"), dataIndex: "name", key: "name" },
    {
      title: t("common.type"),
      dataIndex: "type",
      key: "type",
      render: (type: string) => <Tag color="blue">{type}</Tag>,
    },
    {
      title: t("common.enabled"),
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
            {t("history.testTag")}
          </Button>
          {isOwner && (
            <>
              <Button size="small" onClick={() => openEditModal(record)}>
                {t("common.edit")}
              </Button>
              <Popconfirm
                title={t("channels.deleteConfirm")}
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

  const typeOptions = (typesQuery.data ?? []).map((t) => ({
    value: t.type_name,
    label: t.display_name,
  }));

  return (
    <div>
      {isOwner && (
        <Button type="primary" onClick={openCreateModal} style={{ marginBottom: 16 }}>
          {t("channels.addButton")}
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
        title={editing ? t("channels.editTitle") : t("channels.addButton")}
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
                  rate_limit_per_hour: editing.rate_limit_per_hour ?? undefined,
                  digest_mode: editing.digest_mode,
                  digest_window_minutes: editing.digest_window_minutes,
                }
              : { allow_cross_team_escalation: false, digest_mode: "off", digest_window_minutes: 5 }
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
            <Input />
          </Form.Item>
          <Form.Item
            name="type"
            label={t("common.type")}
            rules={[{ required: true, message: t("channels.typeRequired") }]}
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
            label={t("channels.messageTemplateLabel")}
            help={t("channels.templateHelp")}
          >
            <Select
              allowClear
              loading={templatesQuery.isLoading}
              placeholder={t("channels.inheritDefaultPlaceholder")}
              options={(templatesQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
            />
          </Form.Item>
          <TemplatePreviewPopover template={selectedTemplate} />
          <Form.Item
            name="allow_cross_team_escalation"
            label={t("channels.crossTeamEscalationLabel")}
            valuePropName="checked"
            help={t("channels.crossTeamEscalationHelp")}
          >
            <Switch />
          </Form.Item>

          <Form.Item label={t("channels.stormControlLabel")} style={{ marginBottom: 0 }}>
            <Space direction="vertical" style={{ width: "100%" }} size={0}>
              <Form.Item
                name="digest_mode"
                label={t("channels.digestModeLabel")}
                help={t("channels.digestModeHelp")}
              >
                <Segmented options={DIGEST_MODE_OPTIONS} />
              </Form.Item>
              <Form.Item
                name="rate_limit_per_hour"
                label={t("channels.rateLimitLabel")}
                dependencies={["digest_mode"]}
                rules={[
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      if (getFieldValue("digest_mode") === "auto" && (value === undefined || value === null)) {
                        return Promise.reject(
                          new Error(t("channels.rateLimitRequiredForAuto")),
                        );
                      }
                      return Promise.resolve();
                    },
                  }),
                ]}
              >
                <InputNumber min={1} style={{ width: "100%" }} placeholder={t("channels.unlimitedPlaceholder")} />
              </Form.Item>
              <Form.Item
                name="digest_window_minutes"
                label={t("channels.digestWindowLabel")}
                help={t("channels.digestWindowHelp")}
              >
                <InputNumber min={1} style={{ width: "100%" }} />
              </Form.Item>
            </Space>
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
