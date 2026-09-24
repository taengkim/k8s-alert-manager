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
import { useI18n } from "../i18n";

const { Text } = Typography;

function modeColor(mode: ShareMode): string {
  return mode === "view_notify" ? "green" : "blue";
}

export default function Shares() {
  const { t } = useI18n();
  const { currentTeam, teams } = useTeam();

  if (teams.length === 0 || !currentTeam) {
    return (
      <div>
        <h2>{t("shares.title")}</h2>
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
      <h2>{t("shares.titleWithTeam", { team: currentTeam.name })}</h2>
      <Tabs
        items={[
          { key: "outgoing", label: t("shares.outgoingTab"), children: <OutgoingTab teamId={currentTeam.id} /> },
          { key: "incoming", label: t("shares.incomingTab"), children: <IncomingTab teamId={currentTeam.id} /> },
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
  const { t } = useI18n();
  const MODE_OPTIONS: { value: ShareMode; label: string }[] = [
    { value: "view", label: t("shares.modeViewOnly") },
    { value: "view_notify", label: t("shares.modeViewNotify") },
  ];
  const modeLabel = (mode: ShareMode): string =>
    mode === "view_notify" ? t("shares.modeViewNotify") : t("shares.modeViewOnly");
  const matcherSummary = (matchers: ShareMatcher[] | null): string =>
    !matchers || matchers.length === 0
      ? t("common.all")
      : t("shares.matcherSummaryCount", { count: matchers.length });

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
      message.success(t("shares.createSuccess"));
      queryClient.invalidateQueries({ queryKey: ["shares-outgoing", teamId] });
      closeModal();
    },
    onError: (err) => setFormError(err instanceof ApiError ? err.detail : t("shares.createError")),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, values }: { id: number; values: FormValues }) =>
      updateShare(id, {
        mode: values.mode,
        matchers: values.matchers?.length ? values.matchers : null,
      }),
    onSuccess: () => {
      message.success(t("shares.updateSuccess"));
      queryClient.invalidateQueries({ queryKey: ["shares-outgoing", teamId] });
      closeModal();
    },
    onError: (err) => setFormError(err instanceof ApiError ? err.detail : t("shares.updateError")),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteShare(id),
    onSuccess: () => {
      message.success(t("shares.deleteSuccess"));
      queryClient.invalidateQueries({ queryKey: ["shares-outgoing", teamId] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
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
      title: t("shares.targetTeamColumn"),
      key: "target",
      render: (_: unknown, s: OutgoingShare) => `${s.target_team_name} (${s.target_team_slug})`,
    },
    {
      title: t("shares.modeColumn"),
      dataIndex: "mode",
      key: "mode",
      render: (mode: ShareMode) => <Tag color={modeColor(mode)}>{modeLabel(mode)}</Tag>,
    },
    {
      title: t("shares.scopeColumn"),
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
                  {t("common.edit")}
                </Button>
                <Popconfirm
                  title={t("shares.deleteConfirm")}
                  onConfirm={() => deleteMutation.mutate(s.id)}
                >
                  <Button size="small" danger loading={deleteMutation.isPending}>
                    {t("common.delete")}
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
          {t("shares.createButton")}
        </Button>
      )}
      <Table<OutgoingShare>
        rowKey="id"
        loading={sharesQuery.isLoading}
        dataSource={sharesQuery.data ?? []}
        columns={columns}
        pagination={false}
        locale={{ emptyText: <Empty description={t("shares.emptyOutgoing")} /> }}
      />
      <Modal
        title={editing ? t("shares.editTitle") : t("shares.createButton")}
        open={modalOpen}
        onCancel={closeModal}
        onOk={() => form.submit()}
        okText={t("common.save")}
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
            label={t("shares.targetTeamColumn")}
            rules={[{ required: true, message: t("shares.targetTeamRequired") }]}
          >
            <Select
              disabled={!!editing}
              showSearch
              optionFilterProp="label"
              loading={teamsQuery.isLoading}
              options={targetOptions}
              placeholder={t("shares.targetTeamPlaceholder")}
            />
          </Form.Item>
          <Form.Item name="mode" label={t("shares.modeColumn")} rules={[{ required: true }]}>
            <Radio.Group options={MODE_OPTIONS} optionType="button" />
          </Form.Item>
          <Text type="secondary">{t("shares.modeExplanation")}</Text>
          <div style={{ marginTop: 16, marginBottom: 8 }}>
            <Text strong>{t("shares.scopeMatchersTitle")}</Text>
            <div>
              <Text type="secondary">{t("shares.scopeMatchersHint")}</Text>
            </div>
          </div>
          <MatcherListEditor name="matchers" form={form} />
        </Form>
      </Modal>
    </div>
  );
}

function IncomingTab({ teamId }: { teamId: number }) {
  const { t } = useI18n();
  const modeLabel = (mode: ShareMode): string =>
    mode === "view_notify" ? t("shares.modeViewNotify") : t("shares.modeViewOnly");
  const matcherSummary = (matchers: ShareMatcher[] | null): string =>
    !matchers || matchers.length === 0
      ? t("common.all")
      : t("shares.matcherSummaryCount", { count: matchers.length });

  const sharesQuery = useQuery({
    queryKey: ["shares-incoming", teamId],
    queryFn: () => listIncomingShares(teamId),
  });

  const columns = [
    {
      title: t("shares.ownerTeamColumn"),
      key: "owner",
      render: (_: unknown, s: IncomingShare) => `${s.owner_team_name} (${s.owner_team_slug})`,
    },
    {
      title: t("shares.modeColumn"),
      dataIndex: "mode",
      key: "mode",
      render: (mode: ShareMode) => <Tag color={modeColor(mode)}>{modeLabel(mode)}</Tag>,
    },
    {
      title: t("shares.scopeColumn"),
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
      locale={{ emptyText: <Empty description={t("shares.emptyIncoming")} /> }}
    />
  );
}
