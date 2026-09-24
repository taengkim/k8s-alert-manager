import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Form,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from "antd";
import { useTeam } from "../auth/TeamContext";
import { useDefaultCluster } from "../api/useDefaultCluster";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { listChannels, listEscalationTargets } from "../api/channels";
import {
  createRoute,
  getRoute,
  listNamespaces,
  previewRoute,
  updateRoute,
} from "../api/routes";
import type {
  MatcherKind,
  MatcherTarget,
  RouteAction,
  RoutePreviewItem,
  RouteVerdict,
  RouteWriteInput,
} from "../api/routes";
import { listTemplates } from "../api/templates";
import TemplatePreviewPopover from "./TemplatePreviewPopover";
import MatcherListEditor from "./MatcherListEditor";
import { useI18n } from "../i18n";
import type { TranslationKey } from "../i18n";

const { Text, Title } = Typography;

// Preview only cares about what feeds evaluate() -- not channel_ids (which
// preview never uses), so clicking "최근 알럿에 테스트" shouldn't be blocked
// by "채널을 하나 이상 선택하세요" while a draft's filters are still being
// worked out. "name" stays in this list because the backend's RouteWrite
// schema requires it even for a preview-only draft.
const PREVIEW_VALIDATE_FIELDS = [
  "name",
  "action",
  "severities",
  "namespaces_include",
  "namespaces_exclude",
  "clusters",
  "matchers",
];

const VERDICT_LABEL_KEY: Record<RouteVerdict, TranslationKey> = {
  matched: "testAlert.verdictMatched",
  cluster_filtered: "testAlert.verdictClusterFiltered",
  gated: "testAlert.verdictGated",
  severity_filtered: "testAlert.verdictSeverityFiltered",
  namespace_filtered: "testAlert.verdictNamespaceFiltered",
  not_included: "testAlert.verdictNotIncluded",
  excluded: "testAlert.verdictExcluded",
};

const VERDICT_COLOR: Record<RouteVerdict, string> = {
  matched: "green",
  cluster_filtered: "default",
  gated: "default",
  severity_filtered: "default",
  namespace_filtered: "default",
  not_included: "default",
  excluded: "red",
};

interface MatcherFormValue {
  kind: MatcherKind;
  target: MatcherTarget;
  key?: string;
  pattern: string;
}

interface FormValues {
  name: string;
  description?: string;
  action: RouteAction;
  enabled: boolean;
  notify_on_firing: boolean;
  notify_on_resolved: boolean;
  severities: string[];
  namespaces_include: string[];
  namespaces_exclude: string[];
  clusters: number[];
  channel_ids: number[];
  matchers: MatcherFormValue[];
  template_id?: number;
  include_shared: boolean;
  escalation_enabled: boolean;
  escalation_after_minutes?: number;
  escalation_channel_ids: number[];
  renotify_interval_minutes?: number;
}

interface RouteEditorModalProps {
  open: boolean;
  onClose: () => void;
  /** null/undefined = create mode. */
  routeId?: number | null;
}

function toBody(values: FormValues): RouteWriteInput {
  return {
    name: values.name,
    description: values.description || undefined,
    action: values.action,
    enabled: values.enabled,
    notify_on_firing: values.notify_on_firing,
    notify_on_resolved: values.notify_on_resolved,
    severities: values.severities?.length ? values.severities : undefined,
    namespaces_include: values.namespaces_include?.length ? values.namespaces_include : undefined,
    namespaces_exclude: values.namespaces_exclude?.length ? values.namespaces_exclude : undefined,
    clusters: values.clusters?.length ? values.clusters : undefined,
    channel_ids: values.action === "suppress" ? [] : (values.channel_ids ?? []),
    template_id: values.action === "suppress" ? undefined : values.template_id,
    include_shared: values.include_shared ?? false,
    escalation_enabled: values.action === "notify" ? (values.escalation_enabled ?? false) : false,
    escalation_after_minutes:
      values.action === "notify" && values.escalation_enabled
        ? values.escalation_after_minutes
        : undefined,
    escalation_channel_ids:
      values.action === "notify" && values.escalation_enabled
        ? (values.escalation_channel_ids ?? [])
        : [],
    renotify_interval_minutes:
      values.action === "notify" ? values.renotify_interval_minutes : undefined,
    matchers: (values.matchers ?? []).map((m) => ({
      kind: m.kind,
      target: m.target,
      key: m.target === "alertname" ? undefined : m.key,
      pattern: m.pattern,
    })),
  };
}

export default function RouteEditorModal({ open, onClose, routeId }: RouteEditorModalProps) {
  const { t } = useI18n();
  const isEdit = routeId != null;
  return (
    <Modal
      open={open}
      onCancel={onClose}
      footer={null}
      destroyOnClose
      width={880}
      style={{ top: 24 }}
      styles={{ body: { maxHeight: "calc(100vh - 160px)", overflowY: "auto" } }}
      title={isEdit ? t("routeEditor.editTitle") : t("routeEditor.createTitle")}
    >
      {/* Gated on `open` so this form is a fresh component instance every
          time it's opened -- reopening for a different route (or for
          create, right after editing one) always starts clean. */}
      {open && <RouteEditorFormBody routeId={routeId ?? null} onClose={onClose} />}
    </Modal>
  );
}

function RouteEditorFormBody({
  routeId,
  onClose,
}: {
  routeId: number | null;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const ACTION_OPTIONS: { value: RouteAction; label: string }[] = [
    { value: "notify", label: t("testAlert.actionNotify") },
    { value: "suppress", label: t("testAlert.actionSuppress") },
  ];
  const SEVERITY_OPTIONS = [
    { value: "critical", label: "critical" },
    { value: "warning", label: "warning" },
    { value: "info", label: "info" },
    { value: "none", label: t("common.none") },
  ];
  const isEdit = routeId != null;
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { currentTeam } = useTeam();
  const { cluster } = useDefaultCluster();
  const [form] = Form.useForm<FormValues>();
  const [serverError, setServerError] = useState<string | null>(null);
  const [previewResults, setPreviewResults] = useState<RoutePreviewItem[] | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  const teamId = currentTeam?.id;
  const clusterId = cluster?.id;
  const action = Form.useWatch("action", form) ?? "notify";
  const escalationEnabled = Form.useWatch("escalation_enabled", form) ?? false;

  const routeQuery = useQuery({
    queryKey: ["route", routeId],
    queryFn: () => getRoute(routeId!),
    enabled: isEdit,
  });

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const channelsQuery = useQuery({
    queryKey: ["channels", teamId],
    queryFn: () => listChannels(teamId!),
    enabled: !!teamId,
  });
  const escalationTargetsQuery = useQuery({
    queryKey: ["escalation-targets", teamId],
    queryFn: () => listEscalationTargets(teamId!),
    enabled: !!teamId,
  });
  const namespacesQuery = useQuery({
    queryKey: ["namespaces", clusterId],
    queryFn: () => listNamespaces(clusterId!),
    enabled: !!clusterId,
  });
  const templatesQuery = useQuery({
    queryKey: ["templates", teamId],
    queryFn: () => listTemplates(teamId!),
    enabled: !!teamId,
  });
  const selectedTemplateId = Form.useWatch("template_id", form);
  const selectedTemplate = (templatesQuery.data ?? []).find((t) => t.id === selectedTemplateId);

  useEffect(() => {
    if (!routeQuery.data) return;
    const r = routeQuery.data;
    form.setFieldsValue({
      name: r.name,
      description: r.description ?? undefined,
      action: r.action,
      enabled: r.enabled,
      notify_on_firing: r.notify_on_firing,
      notify_on_resolved: r.notify_on_resolved,
      severities: r.severities ?? [],
      namespaces_include: r.namespaces_include ?? [],
      namespaces_exclude: r.namespaces_exclude ?? [],
      clusters: r.clusters ?? [],
      channel_ids: r.channel_ids,
      template_id: r.template_id ?? undefined,
      include_shared: r.include_shared,
      escalation_enabled: r.escalation_enabled,
      escalation_after_minutes: r.escalation_after_minutes ?? undefined,
      escalation_channel_ids: r.escalation_channel_ids,
      renotify_interval_minutes: r.renotify_interval_minutes ?? undefined,
      matchers: r.matchers.map((m) => ({
        kind: m.kind,
        target: m.target,
        key: m.key ?? undefined,
        pattern: m.pattern,
      })),
    });
  }, [routeQuery.data, form]);

  const saveMutation = useMutation({
    mutationFn: (values: FormValues) => {
      const body = toBody(values);
      return isEdit ? updateRoute(routeId!, body) : createRoute(teamId!, body);
    },
    onSuccess: () => {
      message.success(isEdit ? t("routeEditor.updateSuccess") : t("routeEditor.createSuccess"));
      queryClient.invalidateQueries({ queryKey: ["routes", teamId] });
      onClose();
    },
    onError: (err) => {
      setServerError(
        err instanceof ApiError
          ? err.detail
          : isEdit
            ? t("routeEditor.updateError")
            : t("routeEditor.createError"),
      );
    },
  });

  const handlePreview = async () => {
    if (!teamId) return;
    setPreviewError(null);
    setPreviewLoading(true);
    try {
      // Only the fields evaluate() actually reads are validated -- e.g. an
      // incomplete channel_ids selection shouldn't block trying a draft's
      // filters out. validateFields(subset) only returns that subset, so
      // the full current form state is read separately via getFieldsValue.
      await form.validateFields(PREVIEW_VALIDATE_FIELDS);
      const values = form.getFieldsValue(true) as FormValues;
      const results = await previewRoute(teamId, toBody(values));
      setPreviewResults(results);
    } catch (err) {
      if (err instanceof ApiError) {
        setPreviewError(err.detail);
      } else if (!(err && typeof err === "object" && "errorFields" in err)) {
        setPreviewError(t("preview.loadError"));
      }
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleFinish = (values: FormValues) => {
    setServerError(null);
    saveMutation.mutate(values);
  };

  if (!currentTeam) {
    return <Alert type="info" showIcon message={t("common.noTeamAssigned")} />;
  }

  const channelOptions = (channelsQuery.data ?? []).map((c) => ({
    value: c.id,
    label: c.enabled ? c.name : `${c.name} (${t("common.inactive")})`,
  }));
  const escalationChannelOptions = (escalationTargetsQuery.data ?? []).map((c) => ({
    value: c.id,
    label: c.team_slug === currentTeam.slug ? c.name : `${c.name} (${c.team_slug})`,
  }));
  const clusterOptions = (clustersQuery.data ?? []).map((c) => ({
    value: c.id,
    label: c.display_name,
  }));
  const namespaceOptions = (namespacesQuery.data ?? []).map((ns) => ({ value: ns, label: ns }));

  const previewColumns = [
    { title: t("alerts.alertName"), dataIndex: "alertname", key: "alertname" },
    {
      title: t("common.severity"),
      dataIndex: "severity",
      key: "severity",
      render: (v: string | null) => v ?? "-",
    },
    {
      title: t("common.namespace"),
      dataIndex: "namespace",
      key: "namespace",
      render: (v: string | null) => v ?? "-",
    },
    { title: t("common.cluster"), dataIndex: "cluster", key: "cluster" },
    {
      title: t("routeEditor.currentStatusColumn"),
      dataIndex: "status",
      key: "status",
      render: (v: string) => (v === "firing" ? "firing" : "resolved"),
    },
    {
      title: t("routeEditor.verdictColumn"),
      dataIndex: "verdict",
      key: "verdict",
      render: (verdict: RouteVerdict, row: RoutePreviewItem) => (
        <Space size={4}>
          <Tag color={VERDICT_COLOR[verdict]}>{t(VERDICT_LABEL_KEY[verdict])}</Tag>
          {row.blocking_matcher_position !== null && (
            <Text type="secondary">
              {t("routeEditor.matcherPositionTag", { position: row.blocking_matcher_position + 1 })}
            </Text>
          )}
        </Space>
      ),
    },
  ];

  return (
    <div>
      {serverError && (
        <Alert type="error" showIcon message={serverError} style={{ marginBottom: 16 }} />
      )}

      <Form<FormValues>
        form={form}
        layout="vertical"
        initialValues={{
          action: "notify",
          enabled: true,
          notify_on_firing: true,
          notify_on_resolved: false,
          severities: [],
          namespaces_include: [],
          namespaces_exclude: [],
          clusters: [],
          channel_ids: [],
          matchers: [],
          include_shared: false,
          escalation_enabled: false,
          escalation_channel_ids: [],
        }}
        onFinish={handleFinish}
      >
        <Form.Item
          name="name"
          label={t("common.name")}
          rules={[{ required: true, message: t("common.nameRequired") }]}
        >
          <Input placeholder="critical-to-oncall" />
        </Form.Item>
        <Form.Item name="description" label={t("common.description")}>
          <Input.TextArea rows={2} />
        </Form.Item>

        <Form.Item name="action" label={t("ruleImport.actionColumn")}>
          <Segmented options={ACTION_OPTIONS} />
        </Form.Item>

        <Form.Item name="enabled" label={t("common.enabled")} valuePropName="checked">
          <Switch />
        </Form.Item>

        <Form.Item
          name="include_shared"
          label={t("routeEditor.includeSharedLabel")}
          valuePropName="checked"
          help={t("routeEditor.includeSharedHelp")}
        >
          <Switch />
        </Form.Item>

        <Title level={5}>{t("routeEditor.filtersTitle")}</Title>

        <Form.Item name="severities" label={t("common.severity")} help={t("routeEditor.severityHelp")}>
          <Checkbox.Group options={SEVERITY_OPTIONS} />
        </Form.Item>

        <Form.Item
          name="namespaces_include"
          label={t("routeEditor.namespacesIncludeLabel")}
          help={t("routeEditor.namespaceMatchHelp")}
        >
          <Select
            mode="tags"
            options={namespaceOptions}
            placeholder={t("routeEditor.namespacePlaceholder")}
          />
        </Form.Item>
        <Form.Item
          name="namespaces_exclude"
          label={t("routeEditor.namespacesExcludeLabel")}
          help={t("routeEditor.namespaceMatchHelpShort")}
        >
          <Select
            mode="tags"
            options={namespaceOptions}
            placeholder={t("routeEditor.namespacePlaceholder")}
          />
        </Form.Item>

        <Form.Item name="clusters" label={t("common.cluster")} help={t("routeEditor.clustersHelp")}>
          <Select mode="multiple" options={clusterOptions} placeholder={t("common.allClusters")} />
        </Form.Item>

        <Title level={5}>{t("routeEditor.matchersTitle")}</Title>
        <MatcherListEditor name="matchers" form={form} />

        {action === "notify" && (
          <>
            <Title level={5}>{t("routeEditor.notificationSettingsTitle")}</Title>
            <Form.Item
              name="channel_ids"
              label={t("common.channel")}
              rules={[{ required: true, message: t("routeEditor.channelsRequired") }]}
            >
              <Select mode="multiple" options={channelOptions} placeholder={t("routeEditor.channelSelectPlaceholder")} />
            </Form.Item>
            <Form.Item
              name="template_id"
              label={t("channels.messageTemplateLabel")}
              help={t("routeEditor.templateHelp")}
            >
              <Select
                allowClear
                loading={templatesQuery.isLoading}
                placeholder={t("channels.inheritDefaultPlaceholder")}
                options={(templatesQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
              />
            </Form.Item>
            <TemplatePreviewPopover template={selectedTemplate} />
            <Space direction="vertical" style={{ marginBottom: 16, marginTop: 8 }}>
              <Form.Item name="notify_on_firing" valuePropName="checked" noStyle>
                <Checkbox>{t("routeEditor.notifyOnFiring")}</Checkbox>
              </Form.Item>
              <Form.Item name="notify_on_resolved" valuePropName="checked" noStyle>
                <Checkbox>{t("routeEditor.notifyOnResolved")}</Checkbox>
              </Form.Item>
            </Space>

            <Form.Item
              name="renotify_interval_minutes"
              label={t("routeEditor.renotifyIntervalLabel")}
              help={t("routeEditor.renotifyIntervalHelp")}
            >
              <InputNumber min={1} style={{ width: 200 }} placeholder={t("routeEditor.examplePlaceholder15")} />
            </Form.Item>

            <Title level={5}>{t("routeEditor.escalationTitle")}</Title>
            <Form.Item
              name="escalation_enabled"
              label={t("routeEditor.escalationEnabledLabel")}
              valuePropName="checked"
              help={t("routeEditor.escalationEnabledHelp")}
            >
              <Switch />
            </Form.Item>
            {escalationEnabled && (
              <>
                <Form.Item
                  name="escalation_after_minutes"
                  label={t("routeEditor.escalationAfterLabel")}
                  rules={[{ required: true, message: t("routeEditor.escalationAfterRequired") }]}
                >
                  <InputNumber min={1} style={{ width: 200 }} placeholder={t("routeEditor.examplePlaceholder10")} />
                </Form.Item>
                <Form.Item
                  name="escalation_channel_ids"
                  label={t("routeEditor.escalationChannelsLabel")}
                  help={t("routeEditor.escalationChannelsHelp")}
                  rules={[{ required: true, message: t("routeEditor.escalationChannelsRequired") }]}
                >
                  <Select
                    mode="multiple"
                    loading={escalationTargetsQuery.isLoading}
                    options={escalationChannelOptions}
                    placeholder={t("routeEditor.escalationChannelsPlaceholder")}
                  />
                </Form.Item>
              </>
            )}
          </>
        )}

        <Space>
          <Button type="primary" htmlType="submit" loading={saveMutation.isPending}>
            {t("common.save")}
          </Button>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
        </Space>
      </Form>

      <Title level={5} style={{ marginTop: 32 }}>
        {t("ruleEditor.previewTitle")}
      </Title>
      <Space direction="vertical" style={{ marginBottom: 16 }}>
        <Space>
          <Button onClick={() => void handlePreview()} loading={previewLoading}>
            {t("routeEditor.testAgainstRecentButton")}
          </Button>
          <Text type="secondary">{t("routeEditor.previewScopeHint")}</Text>
        </Space>
        <Text type="secondary">{t("routeEditor.previewStatusHint")}</Text>
      </Space>
      {previewError && (
        <Alert type="error" showIcon message={previewError} style={{ marginBottom: 16 }} />
      )}
      {previewResults && (
        <Table<RoutePreviewItem>
          rowKey="event_id"
          size="small"
          dataSource={previewResults}
          columns={previewColumns}
          pagination={{ pageSize: 20 }}
        />
      )}
    </div>
  );
}
