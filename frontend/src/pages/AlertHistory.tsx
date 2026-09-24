import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs, { type Dayjs } from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import {
  Alert,
  App,
  Badge,
  Button,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Input,
  List,
  Popconfirm,
  Segmented,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { ApiError } from "../api/client";
import { severityColor, severityTagStyle } from "../theme";
import {
  ackAlert,
  addAlertComment,
  deleteAlertComment,
  downloadAlertHistoryExport,
  getAlertComments,
  getAlertHistory,
  getAlertHistoryDetail,
  getAlertHistoryNotifications,
  resolveTestAlert,
  setAlertAssignee,
  unackAlert,
} from "../api/history";
import type {
  AlertEventStatus,
  AlertEventSummary,
  AlertNotificationRecord,
  HistoryExportFilters,
  HistoryExportFormat,
  NotificationStatus,
} from "../api/history";
import { listMembers } from "../api/teams";
import { useI18n } from "../i18n";
import type { TranslationKey } from "../i18n";

dayjs.extend(relativeTime);

const { Text, Title } = Typography;
const { RangePicker } = DatePicker;

type StatusFilter = "all" | AlertEventStatus;

function apiErrorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

const NOTIFICATION_STATUS_COLOR: Record<NotificationStatus, string> = {
  pending: "default",
  in_progress: "processing",
  delivered: "green",
  failed: "orange",
  dead: "red",
  digested: "purple",
};

const NOTIFICATION_STATUS_KEY: Record<NotificationStatus, TranslationKey> = {
  pending: "common.pending",
  in_progress: "history.notifInProgress",
  delivered: "history.notifDelivered",
  failed: "history.notifFailed",
  dead: "history.notifDead",
  digested: "history.notifDigested",
};

export default function AlertHistory() {
  const { t } = useI18n();
  const { message } = App.useApp();
  const { user } = useAuth();
  const { currentTeam, teams } = useTeam();
  const { selectedIds: clusterIds } = useClusterFilter();
  const isAdmin = !!user?.is_admin;
  const queryClient = useQueryClient();

  const SEVERITY_OPTIONS = [
    { value: "critical", label: "critical" },
    { value: "warning", label: "warning" },
    { value: "info", label: "info" },
    { value: "none", label: t("common.none") },
  ];

  /** Phase 15: escalation/renotify trigger values aren't plain enum members
   * -- renotify carries a per-cycle scheduled_action id
   * (`renotify:{id}`) so each cycle's outbox row stays distinct (see
   * app/worker/scheduler.py's _dispatch_renotify) -- so this maps by prefix
   * rather than exact match. */
  const triggerLabel = (trigger: string): string => {
    if (trigger === "firing") return t("history.triggerFiring");
    if (trigger === "resolved") return t("history.triggerResolved");
    if (trigger === "escalation") return t("history.triggerEscalation");
    if (trigger.startsWith("renotify")) return t("history.triggerRenotify");
    if (trigger === "digest") return t("history.triggerDigest");
    return trigger;
  };

  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [severity, setSeverity] = useState<string[]>([]);
  const [namespace, setNamespace] = useState<string | undefined>(undefined);
  const [search, setSearch] = useState("");
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [includeTest, setIncludeTest] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [commentText, setCommentText] = useState("");
  const [exportFormat, setExportFormat] = useState<HistoryExportFormat>("json");
  const [searchParams] = useSearchParams();

  // Deep-link support: the Alerts (live) page's "이력에서 보기" drawer link
  // lands here with ?highlight=<event_id>, opening that row's drawer
  // directly instead of leaving the user to search for it.
  useEffect(() => {
    const highlight = searchParams.get("highlight");
    if (highlight) setSelectedId(Number(highlight));
  }, [searchParams]);

  const teamId = currentTeam?.id;
  const noTeamSelected = !isAdmin && teams.length === 0;
  const fromTs = range?.[0] ? range[0].toISOString() : undefined;
  const toTs = range?.[1] ? range[1].toISOString() : undefined;

  // Shared by the paginated query below and the export button, so an export
  // always reflects exactly the filters currently on screen.
  const currentFilters: HistoryExportFilters = useMemo(
    () => ({
      teamId,
      clusterIds: clusterIds.length > 0 ? clusterIds : undefined,
      status: statusFilter === "all" ? undefined : statusFilter,
      severity: severity.length > 0 ? severity : undefined,
      namespace,
      search: search || undefined,
      fromTs,
      toTs,
      includeTest,
    }),
    [teamId, clusterIds, statusFilter, severity, namespace, search, fromTs, toTs, includeTest],
  );

  const query = useQuery({
    queryKey: ["alert-history", currentFilters, page, pageSize],
    queryFn: () => getAlertHistory({ ...currentFilters, page, pageSize }),
    enabled: isAdmin || !!teamId,
  });

  // Server-side caps mirrored here (app/api/alerts.py's
  // HISTORY_EXPORT_JSON_CAP/HISTORY_EXPORT_NDJSON_CAP) purely so the export
  // button can warn *before* the user waits on a request that's guaranteed
  // to 400 -- the server enforces the real limit regardless.
  const HISTORY_EXPORT_CAP: Record<HistoryExportFormat, number> = {
    json: 10_000,
    ndjson: 100_000,
  };
  const exportOverCap = query.data !== undefined && query.data.total > HISTORY_EXPORT_CAP[exportFormat];

  const exportMutation = useMutation({
    mutationFn: () => downloadAlertHistoryExport(currentFilters, exportFormat),
    onError: (err) => message.error(apiErrorMessage(err, t("common.exportError"))),
  });

  const detailQuery = useQuery({
    queryKey: ["alert-history-detail", selectedId],
    queryFn: () => getAlertHistoryDetail(selectedId as number),
    enabled: selectedId !== null,
  });

  const selected = selectedId !== null ? detailQuery.data : undefined;

  const notificationsQuery = useQuery({
    queryKey: ["alert-history-notifications", selectedId],
    queryFn: () => getAlertHistoryNotifications(selectedId as number),
    enabled: selectedId !== null,
  });

  const commentsQuery = useQuery({
    queryKey: ["alert-comments", selectedId],
    queryFn: () => getAlertComments(selectedId as number),
    enabled: selectedId !== null,
  });

  const membersQuery = useQuery({
    queryKey: ["team-members-for-assignee", selected?.team_id],
    queryFn: () => listMembers(selected!.team_id as number),
    enabled: selected?.team_id != null,
  });

  const invalidateSelected = () => {
    queryClient.invalidateQueries({ queryKey: ["alert-history-detail", selectedId] });
    queryClient.invalidateQueries({ queryKey: ["alert-history"] });
  };

  const ackMutation = useMutation({
    mutationFn: () => ackAlert(selectedId as number),
    onSuccess: invalidateSelected,
    onError: (err) => message.error(apiErrorMessage(err, t("history.ackError"))),
  });
  const unackMutation = useMutation({
    mutationFn: () => unackAlert(selectedId as number),
    onSuccess: invalidateSelected,
    onError: (err) => message.error(apiErrorMessage(err, t("history.unackError"))),
  });
  const assigneeMutation = useMutation({
    mutationFn: (userId: number | null) => setAlertAssignee(selectedId as number, userId),
    onSuccess: invalidateSelected,
    onError: (err) => message.error(apiErrorMessage(err, t("history.assigneeError"))),
  });
  const resolveTestMutation = useMutation({
    mutationFn: () => resolveTestAlert(selectedId as number),
    onSuccess: () => {
      invalidateSelected();
      queryClient.invalidateQueries({ queryKey: ["alert-history-notifications", selectedId] });
      message.success(t("history.testResolvedSuccess"));
    },
    onError: (err) => message.error(apiErrorMessage(err, t("history.testResolveError"))),
  });
  const addCommentMutation = useMutation({
    mutationFn: (body: string) => addAlertComment(selectedId as number, body),
    onSuccess: () => {
      setCommentText("");
      queryClient.invalidateQueries({ queryKey: ["alert-comments", selectedId] });
    },
    onError: (err) => message.error(apiErrorMessage(err, t("history.commentAddError"))),
  });
  const deleteCommentMutation = useMutation({
    mutationFn: (commentId: number) => deleteAlertComment(commentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alert-comments", selectedId] }),
    onError: (err) => message.error(apiErrorMessage(err, t("history.commentDeleteError"))),
  });

  const items = useMemo(() => query.data?.items ?? [], [query.data]);
  const total = query.data?.total ?? 0;

  // Sourced from the currently loaded page only (same trade-off Alerts.tsx
  // makes for its live namespace filter) -- there's no distinct-namespaces
  // endpoint, and adding one is out of scope for this phase.
  const namespaceOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const item of items) {
      if (item.namespace) seen.add(item.namespace);
    }
    return Array.from(seen)
      .sort()
      .map((ns) => ({ value: ns, label: ns }));
  }, [items]);

  const isTeamOwner = useMemo(() => {
    if (!user || selected?.team_id == null) return false;
    if (user.is_admin) return true;
    return user.teams.find((t) => t.id === selected.team_id)?.role === "owner";
  }, [user, selected]);

  const resetToFirstPage = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value);
    setPage(1);
  };

  if (noTeamSelected) {
    return (
      <div>
        <h2>{t("history.title")}</h2>
        <Alert
          type="info"
          showIcon
          message={t("common.noTeamAssigned")}
          description={t("common.requestTeamAssignment")}
        />
      </div>
    );
  }

  const columns = [
    {
      title: t("common.status"),
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (value: AlertEventStatus) =>
        value === "firing" ? (
          <Badge status="processing" color={severityColor("critical")} text="firing" />
        ) : (
          <Badge status="default" text="resolved" />
        ),
    },
    {
      title: t("alerts.alertName"),
      dataIndex: "alertname",
      key: "alertname",
      render: (value: string, record: AlertEventSummary) => (
        <Space size={4}>
          {value}
          {record.is_test && <Tag color="purple">{t("history.testTag")}</Tag>}
          {record.shared_from && (
            <Tag color="blue">{t("alerts.sharedFrom", { source: record.shared_from })}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("common.severity"),
      dataIndex: "severity",
      key: "severity",
      render: (value: string | null) => <Tag style={severityTagStyle(value)}>{value ?? "none"}</Tag>,
    },
    {
      title: t("common.namespace"),
      dataIndex: "namespace",
      key: "namespace",
      render: (value: string | null) => value ?? "-",
    },
    { title: t("common.cluster"), dataIndex: "cluster_name", key: "cluster_name" },
    {
      title: t("common.acknowledged"),
      key: "acknowledged",
      width: 70,
      align: "center" as const,
      render: (_: unknown, record: AlertEventSummary) =>
        record.acknowledged_at ? (
          <Tooltip
            title={`${record.acknowledged_by?.username ?? "?"} · ${dayjs(record.acknowledged_at).format("YYYY-MM-DD HH:mm:ss")}`}
          >
            <Tag color="success">✓</Tag>
          </Tooltip>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    {
      title: t("common.assignee"),
      key: "assignee",
      width: 100,
      render: (_: unknown, record: AlertEventSummary) =>
        record.assignee?.username ?? <Text type="secondary">-</Text>,
    },
    { title: t("history.receiveCount"), dataIndex: "receive_count", key: "receive_count", width: 100 },
    {
      title: t("common.startedAt"),
      dataIndex: "starts_at",
      key: "starts_at",
      render: (value: string) => (
        <Tooltip title={dayjs(value).format("YYYY-MM-DD HH:mm:ss")}>{dayjs(value).fromNow()}</Tooltip>
      ),
    },
    {
      title: t("history.lastReceivedAt"),
      dataIndex: "last_received_at",
      key: "last_received_at",
      render: (value: string) => (
        <Tooltip title={dayjs(value).format("YYYY-MM-DD HH:mm:ss")}>{dayjs(value).fromNow()}</Tooltip>
      ),
    },
  ];

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <h2 style={{ margin: 0 }}>
          {t("history.title")}{" "}
          <Text type="secondary" style={{ fontSize: 14, fontWeight: "normal" }}>
            ({t("alerts.count", { count: total })})
          </Text>
        </h2>
        <Space>
          <Select<HistoryExportFormat>
            value={exportFormat}
            onChange={setExportFormat}
            style={{ width: 110 }}
            options={[
              { value: "json", label: "JSON" },
              { value: "ndjson", label: "NDJSON" },
            ]}
          />
          <Button
            loading={exportMutation.isPending}
            disabled={exportOverCap}
            onClick={() => exportMutation.mutate()}
          >
            {t("history.exportButton", { format: exportFormat.toUpperCase() })}
          </Button>
        </Space>
      </div>

      {exportOverCap && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={t("history.exportCapExceeded", {
            total,
            format: exportFormat.toUpperCase(),
            cap: HISTORY_EXPORT_CAP[exportFormat].toLocaleString(),
          })}
        />
      )}

      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <Segmented<StatusFilter>
          value={statusFilter}
          onChange={resetToFirstPage(setStatusFilter)}
          options={[
            { label: t("common.all"), value: "all" },
            { label: "firing", value: "firing" },
            { label: "resolved", value: "resolved" },
          ]}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder={t("common.severity")}
          style={{ minWidth: 220 }}
          options={SEVERITY_OPTIONS}
          value={severity}
          onChange={resetToFirstPage(setSeverity)}
        />
        <Select
          allowClear
          placeholder={t("common.namespace")}
          style={{ minWidth: 200 }}
          options={namespaceOptions}
          value={namespace}
          onChange={resetToFirstPage(setNamespace)}
        />
        <Input.Search
          placeholder={t("alerts.searchPlaceholder")}
          allowClear
          style={{ minWidth: 240 }}
          onSearch={resetToFirstPage(setSearch)}
        />
        <RangePicker showTime value={range} onChange={resetToFirstPage(setRange)} />
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Switch checked={includeTest} onChange={resetToFirstPage(setIncludeTest)} />
          <Text>{t("history.includeTest")}</Text>
        </div>
      </div>

      <Table<AlertEventSummary>
        rowKey="id"
        loading={query.isLoading}
        dataSource={items}
        columns={columns}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onChange: (nextPage, nextPageSize) => {
            setPage(nextPage);
            setPageSize(nextPageSize);
          },
        }}
        onRow={(record) => ({
          onClick: () => setSelectedId(record.id),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("history.empty")} /> }}
      />

      <Drawer
        title={
          <Space>
            {selected?.alertname}
            {selected?.is_test && <Tag color="purple">{t("history.testTag")}</Tag>}
            {selected?.shared_from && (
              <Tag color="blue">{t("alerts.sharedFrom", { source: selected.shared_from })}</Tag>
            )}
          </Space>
        }
        open={selectedId !== null}
        onClose={() => {
          setSelectedId(null);
          setCommentText("");
        }}
        width={520}
        loading={detailQuery.isLoading}
      >
        {selected && (
          <>
            {selected.is_test && selected.status === "firing" && (
              <Alert
                type="warning"
                showIcon
                style={{ marginBottom: 16 }}
                message={t("history.testAlertBanner")}
                action={
                  <Button
                    size="small"
                    loading={resolveTestMutation.isPending}
                    onClick={() => resolveTestMutation.mutate()}
                  >
                    {t("history.resolveNow")}
                  </Button>
                }
              />
            )}

            <Descriptions column={1} bordered size="small" style={{ marginBottom: 24 }}>
              <Descriptions.Item label={t("common.status")}>{selected.status}</Descriptions.Item>
              <Descriptions.Item label={t("common.severity")}>
                <Tag style={severityTagStyle(selected.severity)}>{selected.severity ?? "none"}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("common.namespace")}>{selected.namespace ?? "-"}</Descriptions.Item>
              <Descriptions.Item label={t("common.cluster")}>{selected.cluster_name}</Descriptions.Item>
              <Descriptions.Item label={t("common.startedAt")}>
                {dayjs(selected.starts_at).format("YYYY-MM-DD HH:mm:ss")}
              </Descriptions.Item>
              <Descriptions.Item label={t("history.firstReceivedAt")}>
                {dayjs(selected.first_received_at).format("YYYY-MM-DD HH:mm:ss")}
              </Descriptions.Item>
              <Descriptions.Item label={t("history.lastReceivedAt")}>
                {dayjs(selected.last_received_at).format("YYYY-MM-DD HH:mm:ss")}
              </Descriptions.Item>
              <Descriptions.Item label={t("history.receiveCount")}>{selected.receive_count}</Descriptions.Item>
              <Descriptions.Item label={t("common.acknowledged")}>
                {selected.acknowledged_at ? (
                  <Space>
                    <Tag color="success">
                      {selected.acknowledged_by?.username} ·{" "}
                      {dayjs(selected.acknowledged_at).format("YYYY-MM-DD HH:mm:ss")}
                    </Tag>
                    <Button
                      size="small"
                      loading={unackMutation.isPending}
                      onClick={() => unackMutation.mutate()}
                    >
                      {t("history.unacknowledge")}
                    </Button>
                  </Space>
                ) : (
                  <Button
                    size="small"
                    type="primary"
                    loading={ackMutation.isPending}
                    onClick={() => ackMutation.mutate()}
                  >
                    {t("history.ackButton")}
                  </Button>
                )}
              </Descriptions.Item>
              <Descriptions.Item label={t("common.assignee")}>
                <Select
                  allowClear
                  placeholder={t("history.assignPlaceholder")}
                  style={{ minWidth: 220 }}
                  disabled={selected.team_id == null}
                  loading={membersQuery.isLoading}
                  value={selected.assignee?.id}
                  options={(membersQuery.data ?? []).map((m) => ({
                    value: m.user_id,
                    label: m.display_name,
                  }))}
                  onChange={(value) => assigneeMutation.mutate(value ?? null)}
                />
              </Descriptions.Item>
            </Descriptions>

            <Title level={5}>{t("alerts.labelsTitle")}</Title>
            <div style={{ marginBottom: 24 }}>
              {Object.entries(selected.labels).map(([key, value]) => (
                <Tag key={key} style={{ marginBottom: 4 }}>
                  {key}={value}
                </Tag>
              ))}
            </div>

            <Title level={5}>{t("alerts.annotationsTitle")}</Title>
            <div style={{ whiteSpace: "pre-wrap", marginBottom: 24 }}>
              {Object.entries(selected.annotations).length > 0 ? (
                Object.entries(selected.annotations).map(([key, value]) => (
                  <div key={key} style={{ marginBottom: 8 }}>
                    <Text strong>{key}: </Text>
                    {value}
                  </div>
                ))
              ) : (
                <Text type="secondary">-</Text>
              )}
            </div>

            <Space size="middle">
              {selected.generator_url && (
                <a href={selected.generator_url} target="_blank" rel="noreferrer">
                  {t("alerts.viewInPrometheus")}
                </a>
              )}
              {selected.grafana_url && (
                <a href={selected.grafana_url} target="_blank" rel="noreferrer">
                  {t("alerts.viewInGrafana")}
                </a>
              )}
            </Space>

            <Title level={5} style={{ marginTop: 24 }}>
              {t("history.commentsTitle")}
            </Title>
            <List
              size="small"
              loading={commentsQuery.isLoading}
              dataSource={commentsQuery.data ?? []}
              locale={{ emptyText: <Empty description={t("history.noComments")} /> }}
              style={{ marginBottom: 12 }}
              renderItem={(comment) => {
                const canDelete =
                  isTeamOwner || isAdmin || comment.user?.id === user?.id;
                return (
                  <List.Item
                    actions={
                      canDelete
                        ? [
                            <Popconfirm
                              key="delete"
                              title={t("history.deleteCommentConfirm")}
                              onConfirm={() => deleteCommentMutation.mutate(comment.id)}
                            >
                              <Button size="small" type="text" danger>
                                {t("common.delete")}
                              </Button>
                            </Popconfirm>,
                          ]
                        : []
                    }
                  >
                    <List.Item.Meta
                      title={
                        <Space>
                          <Text strong>{comment.user?.display_name ?? t("history.deletedUser")}</Text>
                          <Text type="secondary" style={{ fontWeight: "normal", fontSize: 12 }}>
                            {dayjs(comment.created_at).format("YYYY-MM-DD HH:mm:ss")}
                          </Text>
                        </Space>
                      }
                      description={<div style={{ whiteSpace: "pre-wrap" }}>{comment.body}</div>}
                    />
                  </List.Item>
                );
              }}
            />
            <Space.Compact style={{ width: "100%", marginBottom: 24 }}>
              <Input.TextArea
                rows={2}
                placeholder={t("history.commentPlaceholder")}
                value={commentText}
                onChange={(e) => setCommentText(e.target.value)}
              />
              <Button
                type="primary"
                loading={addCommentMutation.isPending}
                disabled={!commentText.trim()}
                onClick={() => addCommentMutation.mutate(commentText)}
              >
                {t("history.postComment")}
              </Button>
            </Space.Compact>

            <Title level={5}>{t("history.notificationHistoryTitle")}</Title>
            <Table<AlertNotificationRecord>
              rowKey="id"
              size="small"
              loading={notificationsQuery.isLoading}
              dataSource={notificationsQuery.data ?? []}
              pagination={false}
              locale={{ emptyText: <Empty description={t("history.noNotifications")} /> }}
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
                  render: (value: NotificationStatus, record: AlertNotificationRecord) => {
                    const statusTag = (
                      <Tag color={NOTIFICATION_STATUS_COLOR[value]}>
                        {t(NOTIFICATION_STATUS_KEY[value])}
                      </Tag>
                    );
                    // Phase 16: a 'digested' row was folded into an
                    // aggregate digest send -- show that aggregate's own
                    // (normal) delivery status alongside, rather than
                    // leaving "다이제스트로 묶임" looking like a dead end.
                    if (value !== "digested" || !record.digested_into) return statusTag;
                    const agg = record.digested_into;
                    return (
                      <Space size={4}>
                        {statusTag}
                        <Tooltip
                          title={
                            agg.delivered_at
                              ? t("history.deliveredAtLabel", {
                                  time: dayjs(agg.delivered_at).format("YYYY-MM-DD HH:mm:ss"),
                                })
                              : undefined
                          }
                        >
                          <Tag color={NOTIFICATION_STATUS_COLOR[agg.status]}>
                            {t("history.aggregateSummary", {
                              count: agg.notification_count ?? "?",
                              status: t(NOTIFICATION_STATUS_KEY[agg.status]),
                            })}
                          </Tag>
                        </Tooltip>
                      </Space>
                    );
                  },
                },
                { title: t("history.attemptsColumn"), dataIndex: "attempts", key: "attempts" },
                {
                  title: t("history.deliveredAtColumn"),
                  dataIndex: "delivered_at",
                  key: "delivered_at",
                  render: (value: string | null) =>
                    value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "-",
                },
                {
                  title: t("history.errorColumn"),
                  dataIndex: "last_error",
                  key: "last_error",
                  render: (value: string | null) =>
                    value ? (
                      <Tooltip title={value}>
                        <Text type="danger" ellipsis style={{ maxWidth: 200, display: "inline-block" }}>
                          {value}
                        </Text>
                      </Tooltip>
                    ) : (
                      "-"
                    ),
                },
              ]}
            />
          </>
        )}
      </Drawer>
    </div>
  );
}
