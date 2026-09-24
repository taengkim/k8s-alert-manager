import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { Alert, Button, Form, Input, Modal, Select, Space, Table, Tag, Typography } from "antd";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { getAlertHistoryNotifications } from "../api/history";
import type { NotificationStatus } from "../api/history";
import { fireTestAlert } from "../api/testAlert";
import type { RouteVerdict } from "../api/routes";
import type { TestAlertInput, TestAlertResult, TestAlertVerdict } from "../api/testAlert";
import { useI18n } from "../i18n";
import type { TranslationKey } from "../i18n";

const { Text } = Typography;

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

const NOTIFICATION_STATUS_KEY: Record<NotificationStatus, TranslationKey> = {
  pending: "common.pending",
  in_progress: "history.notifInProgress",
  delivered: "history.notifDelivered",
  failed: "history.notifFailed",
  dead: "history.notifDead",
  // Phase 16: a test alert can be parked by storm control same as any
  // other notification (see app.services.routing.stage_outbox_row's
  // docstring -- parking is channel-level, not trigger-specific).
  digested: "history.notifDigested",
};

const NOTIFICATION_STATUS_COLOR: Record<NotificationStatus, string> = {
  pending: "default",
  in_progress: "processing",
  delivered: "green",
  failed: "orange",
  dead: "red",
  digested: "purple",
};

interface LabelPair {
  key: string;
  value: string;
}

interface TestAlertFormValues {
  cluster_id: number;
  alertname: string;
  severity: string;
  namespace?: string;
  labels?: LabelPair[];
}

interface TestAlertModalProps {
  open: boolean;
  onClose: () => void;
  teamId: number;
}

export default function TestAlertModal({ open, onClose, teamId }: TestAlertModalProps) {
  const { t } = useI18n();

  /** Phase 15: renotify carries a per-cycle scheduled_action id
   * (`renotify:{id}`), so this maps by prefix rather than exact match --
   * same convention as AlertHistory.tsx's triggerLabel. */
  const triggerLabel = (trigger: string): string => {
    if (trigger === "firing") return t("history.triggerFiring");
    if (trigger === "resolved") return t("history.triggerResolved");
    if (trigger === "escalation") return t("history.triggerEscalation");
    if (trigger.startsWith("renotify")) return t("history.triggerRenotify");
    if (trigger === "digest") return t("history.triggerDigest");
    return trigger;
  };

  const [form] = Form.useForm<TestAlertFormValues>();
  const [result, setResult] = useState<TestAlertResult | null>(null);

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });

  // 5s-interval polling of the fired test event's delivery status, so the
  // operator can watch pending -> delivered without leaving the modal.
  // Gated on `open` too, not just `result` -- this component stays mounted
  // in Routes.tsx regardless of the Modal's own open state (`destroyOnClose`
  // only unmounts its children), so without the `open` gate the interval
  // would keep polling in the background indefinitely after the modal is
  // closed.
  const notificationsQuery = useQuery({
    queryKey: ["alert-history-notifications", result?.event_id],
    queryFn: () => getAlertHistoryNotifications(result!.event_id),
    enabled: open && result !== null,
    refetchInterval: 5000,
  });

  useEffect(() => {
    if (open) {
      setResult(null);
      form.resetFields();
    }
  }, [open, form]);

  const mutation = useMutation({
    mutationFn: (values: TestAlertFormValues) => {
      const labels: Record<string, string> = {};
      for (const pair of values.labels ?? []) {
        if (pair?.key) labels[pair.key] = pair.value ?? "";
      }
      const body: TestAlertInput = {
        cluster_id: values.cluster_id,
        alertname: values.alertname,
        severity: values.severity,
        namespace: values.namespace || undefined,
        labels,
      };
      return fireTestAlert(teamId, body);
    },
    onSuccess: (data) => setResult(data),
  });

  const handleClose = () => {
    mutation.reset();
    setResult(null);
    onClose();
  };

  return (
    <Modal
      title={t("testAlert.modalTitle")}
      open={open}
      onCancel={handleClose}
      footer={
        result
          ? [
              <Button key="again" onClick={() => setResult(null)}>
                {t("testAlert.fireAgain")}
              </Button>,
              <Button key="close" type="primary" onClick={handleClose}>
                {t("common.close")}
              </Button>,
            ]
          : [
              <Button key="cancel" onClick={handleClose}>
                {t("common.cancel")}
              </Button>,
              <Button
                key="submit"
                type="primary"
                loading={mutation.isPending}
                onClick={() => form.submit()}
              >
                {t("testAlert.fireButton")}
              </Button>,
            ]
      }
      destroyOnClose
      width={640}
    >
      {mutation.isError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            mutation.error instanceof ApiError
              ? mutation.error.detail
              : t("testAlert.fireError")
          }
        />
      )}

      {!result ? (
        <Form
          form={form}
          layout="vertical"
          initialValues={{ alertname: "KamTestAlert", severity: "warning" }}
          onFinish={(values) => mutation.mutate(values)}
        >
          <Form.Item
            name="cluster_id"
            label={t("common.cluster")}
            rules={[{ required: true, message: t("ruleEditor.selectClusterPrompt") }]}
          >
            <Select
              loading={clustersQuery.isLoading}
              options={(clustersQuery.data ?? []).map((c) => ({
                value: c.id,
                label: c.display_name,
              }))}
            />
          </Form.Item>
          <Form.Item name="alertname" label={t("alerts.alertName")} rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="severity" label={t("common.severity")} rules={[{ required: true }]}>
            <Select
              options={[
                { value: "critical", label: "critical" },
                { value: "warning", label: "warning" },
                { value: "info", label: "info" },
              ]}
            />
          </Form.Item>
          <Form.Item name="namespace" label={t("common.namespace")}>
            <Input placeholder={t("common.optional")} />
          </Form.Item>
          <Form.List name="labels">
            {(fields, { add, remove }) => (
              <Form.Item label={t("testAlert.extraLabelsLabel")}>
                {fields.map((field) => (
                  <div key={field.key} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                    <Form.Item name={[field.name, "key"]} noStyle>
                      <Input placeholder="key" style={{ flex: 1 }} />
                    </Form.Item>
                    <Form.Item name={[field.name, "value"]} noStyle>
                      <Input placeholder="value" style={{ flex: 1 }} />
                    </Form.Item>
                    <Button type="text" danger onClick={() => remove(field.name)}>
                      {t("common.delete")}
                    </Button>
                  </div>
                ))}
                <Button block onClick={() => add({ key: "", value: "" })}>
                  {t("ruleEditor.addLabel")}
                </Button>
              </Form.Item>
            )}
          </Form.List>
        </Form>
      ) : (
        <div>
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              <Space>
                {t("testAlert.fireSuccess")}
                <Link to={`/alerts/history?highlight=${result.event_id}`}>{t("alerts.viewInHistory")}</Link>
              </Space>
            }
          />

          {result.suppressed_by && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 16 }}
              message={t("testAlert.suppressedByRule", { name: result.suppressed_by.rule_name })}
              description={t("testAlert.suppressedByRuleDesc", {
                matchedLabel: t("testAlert.verdictMatched"),
              })}
            />
          )}

          <Text strong>{t("testAlert.ruleEvalResultsTitle")}</Text>
          <Table
            size="small"
            style={{ marginTop: 8, marginBottom: 16 }}
            rowKey="rule_id"
            dataSource={result.verdicts}
            pagination={false}
            locale={{ emptyText: t("testAlert.noActiveRules") }}
            columns={[
              { title: t("testAlert.ruleColumn"), dataIndex: "rule_name", key: "rule_name" },
              {
                title: t("ruleImport.actionColumn"),
                dataIndex: "action",
                key: "action",
                render: (action: string) => (
                  <Tag color={action === "suppress" ? "red" : "blue"}>
                    {action === "suppress" ? t("testAlert.actionSuppress") : t("testAlert.actionNotify")}
                  </Tag>
                ),
              },
              {
                title: t("testAlert.resultColumn"),
                dataIndex: "verdict",
                key: "verdict",
                render: (verdict: RouteVerdict, row: TestAlertVerdict) => {
                  // A notify rule's own evaluate() call has no visibility
                  // into route_event's suppress-wins-exclusively
                  // short-circuit -- so "matched" here can be true even
                  // though nothing was actually delivered. Flag that case
                  // instead of presenting it as if it had notified.
                  const suppressedMatch =
                    !!result.suppressed_by && row.action === "notify" && verdict === "matched";
                  if (suppressedMatch) {
                    return <Tag color="default">{t("testAlert.matchedButSuppressed")}</Tag>;
                  }
                  return <Tag color={VERDICT_COLOR[verdict]}>{t(VERDICT_LABEL_KEY[verdict])}</Tag>;
                },
              },
            ]}
          />

          <Text strong>{t("testAlert.deliveredChannelsTitle")}</Text>
          <div style={{ margin: "8px 0 16px" }}>
            {result.delivered_channels.length > 0 ? (
              result.delivered_channels.map((name) => (
                <Tag key={name} color="blue">
                  {name}
                </Tag>
              ))
            ) : (
              <Text type="secondary">{t("testAlert.noMatchingNotifyRules")}</Text>
            )}
          </div>

          <Text strong>{t("testAlert.deliveryStatusTitle")}</Text>
          {notificationsQuery.isError ? (
            <Alert
              type="error"
              showIcon
              style={{ marginTop: 8 }}
              message={t("testAlert.deliveryStatusLoadError")}
            />
          ) : (
            <Table
              size="small"
              style={{ marginTop: 8 }}
              rowKey="id"
              loading={notificationsQuery.isLoading}
              dataSource={notificationsQuery.data ?? []}
              pagination={false}
              locale={{ emptyText: t("testAlert.noTargetChannels") }}
              columns={[
                { title: t("common.channel"), dataIndex: "channel_name", key: "channel_name" },
                {
                  title: t("history.triggerColumn"),
                  dataIndex: "trigger",
                  key: "trigger",
                  render: (value: string) => triggerLabel(value),
                },
                {
                  title: t("common.status"),
                  dataIndex: "status",
                  key: "status",
                  render: (value: NotificationStatus) => (
                    <Tag color={NOTIFICATION_STATUS_COLOR[value]}>
                      {t(NOTIFICATION_STATUS_KEY[value])}
                    </Tag>
                  ),
                },
              ]}
            />
          )}
        </div>
      )}
    </Modal>
  );
}
