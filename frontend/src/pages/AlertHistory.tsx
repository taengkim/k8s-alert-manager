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

dayjs.extend(relativeTime);

const { Text, Title } = Typography;
const { RangePicker } = DatePicker;

type StatusFilter = "all" | AlertEventStatus;

const SEVERITY_OPTIONS = [
  { value: "critical", label: "critical" },
  { value: "warning", label: "warning" },
  { value: "info", label: "info" },
  { value: "none", label: "없음" },
];

const SEVERITY_TAG_COLOR: Record<string, string> = {
  critical: "red",
  warning: "orange",
  info: "blue",
};

function severityColor(severity: string | null): string {
  return (severity && SEVERITY_TAG_COLOR[severity]) ?? "default";
}

function apiErrorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

const NOTIFICATION_STATUS_COLOR: Record<NotificationStatus, string> = {
  pending: "default",
  in_progress: "processing",
  delivered: "green",
  failed: "orange",
  dead: "red",
};

const NOTIFICATION_STATUS_LABEL: Record<NotificationStatus, string> = {
  pending: "대기",
  in_progress: "발송 중",
  delivered: "발송 완료",
  failed: "실패",
  dead: "포기됨",
};

export default function AlertHistory() {
  const { message } = App.useApp();
  const { user } = useAuth();
  const { currentTeam, teams } = useTeam();
  const { selectedIds: clusterIds } = useClusterFilter();
  const isAdmin = !!user?.is_admin;
  const queryClient = useQueryClient();

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
    onError: (err) => message.error(apiErrorMessage(err, "내보내기에 실패했습니다")),
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
    onError: (err) => message.error(apiErrorMessage(err, "확인 처리에 실패했습니다")),
  });
  const unackMutation = useMutation({
    mutationFn: () => unackAlert(selectedId as number),
    onSuccess: invalidateSelected,
    onError: (err) => message.error(apiErrorMessage(err, "확인 취소에 실패했습니다")),
  });
  const assigneeMutation = useMutation({
    mutationFn: (userId: number | null) => setAlertAssignee(selectedId as number, userId),
    onSuccess: invalidateSelected,
    onError: (err) => message.error(apiErrorMessage(err, "담당자 지정에 실패했습니다")),
  });
  const resolveTestMutation = useMutation({
    mutationFn: () => resolveTestAlert(selectedId as number),
    onSuccess: () => {
      invalidateSelected();
      queryClient.invalidateQueries({ queryKey: ["alert-history-notifications", selectedId] });
      message.success("테스트 알럿을 해제했습니다");
    },
    onError: (err) => message.error(apiErrorMessage(err, "테스트 알럿 해제에 실패했습니다")),
  });
  const addCommentMutation = useMutation({
    mutationFn: (body: string) => addAlertComment(selectedId as number, body),
    onSuccess: () => {
      setCommentText("");
      queryClient.invalidateQueries({ queryKey: ["alert-comments", selectedId] });
    },
    onError: (err) => message.error(apiErrorMessage(err, "댓글 작성에 실패했습니다")),
  });
  const deleteCommentMutation = useMutation({
    mutationFn: (commentId: number) => deleteAlertComment(commentId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alert-comments", selectedId] }),
    onError: (err) => message.error(apiErrorMessage(err, "댓글 삭제에 실패했습니다")),
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
        <h2>알럿 이력</h2>
        <Alert
          type="info"
          showIcon
          message="소속된 팀이 없습니다"
          description="관리자에게 팀 추가를 요청하세요."
        />
      </div>
    );
  }

  const columns = [
    {
      title: "상태",
      dataIndex: "status",
      key: "status",
      width: 110,
      render: (value: AlertEventStatus) =>
        value === "firing" ? (
          <Badge status="processing" color="red" text="firing" />
        ) : (
          <Badge status="default" text="resolved" />
        ),
    },
    {
      title: "알럿명",
      dataIndex: "alertname",
      key: "alertname",
      render: (value: string, record: AlertEventSummary) => (
        <Space size={4}>
          {value}
          {record.is_test && <Tag color="purple">테스트</Tag>}
          {record.shared_from && <Tag color="blue">공유: {record.shared_from}</Tag>}
        </Space>
      ),
    },
    {
      title: "심각도",
      dataIndex: "severity",
      key: "severity",
      render: (value: string | null) => <Tag color={severityColor(value)}>{value ?? "none"}</Tag>,
    },
    {
      title: "네임스페이스",
      dataIndex: "namespace",
      key: "namespace",
      render: (value: string | null) => value ?? "-",
    },
    { title: "클러스터", dataIndex: "cluster_name", key: "cluster_name" },
    {
      title: "확인",
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
      title: "담당자",
      key: "assignee",
      width: 100,
      render: (_: unknown, record: AlertEventSummary) =>
        record.assignee?.username ?? <Text type="secondary">-</Text>,
    },
    { title: "수신 횟수", dataIndex: "receive_count", key: "receive_count", width: 100 },
    {
      title: "시작 시각",
      dataIndex: "starts_at",
      key: "starts_at",
      render: (value: string) => (
        <Tooltip title={dayjs(value).format("YYYY-MM-DD HH:mm:ss")}>{dayjs(value).fromNow()}</Tooltip>
      ),
    },
    {
      title: "최종 수신",
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
          알럿 이력{" "}
          <Text type="secondary" style={{ fontSize: 14, fontWeight: "normal" }}>
            ({total}건)
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
            {exportFormat === "json" ? "JSON 내보내기" : "NDJSON 내보내기"}
          </Button>
        </Space>
      </div>

      {exportOverCap && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 16 }}
          message={`현재 필터 결과(${total}건)가 ${exportFormat.toUpperCase()} 내보내기 캡(${HISTORY_EXPORT_CAP[exportFormat].toLocaleString()}건)을 초과합니다. 필터를 좁혀주세요.`}
        />
      )}

      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <Segmented<StatusFilter>
          value={statusFilter}
          onChange={resetToFirstPage(setStatusFilter)}
          options={[
            { label: "전체", value: "all" },
            { label: "firing", value: "firing" },
            { label: "resolved", value: "resolved" },
          ]}
        />
        <Select
          mode="multiple"
          allowClear
          placeholder="심각도"
          style={{ minWidth: 220 }}
          options={SEVERITY_OPTIONS}
          value={severity}
          onChange={resetToFirstPage(setSeverity)}
        />
        <Select
          allowClear
          placeholder="네임스페이스"
          style={{ minWidth: 200 }}
          options={namespaceOptions}
          value={namespace}
          onChange={resetToFirstPage(setNamespace)}
        />
        <Input.Search
          placeholder="알럿명 검색"
          allowClear
          style={{ minWidth: 240 }}
          onSearch={resetToFirstPage(setSearch)}
        />
        <RangePicker showTime value={range} onChange={resetToFirstPage(setRange)} />
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <Switch checked={includeTest} onChange={resetToFirstPage(setIncludeTest)} />
          <Text>테스트 포함</Text>
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
        locale={{ emptyText: <Empty description="이력이 없습니다" /> }}
      />

      <Drawer
        title={
          <Space>
            {selected?.alertname}
            {selected?.is_test && <Tag color="purple">테스트</Tag>}
            {selected?.shared_from && <Tag color="blue">공유: {selected.shared_from}</Tag>}
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
                message="테스트 알럿입니다"
                action={
                  <Button
                    size="small"
                    loading={resolveTestMutation.isPending}
                    onClick={() => resolveTestMutation.mutate()}
                  >
                    지금 해제
                  </Button>
                }
              />
            )}

            <Descriptions column={1} bordered size="small" style={{ marginBottom: 24 }}>
              <Descriptions.Item label="상태">{selected.status}</Descriptions.Item>
              <Descriptions.Item label="심각도">
                <Tag color={severityColor(selected.severity)}>{selected.severity ?? "none"}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="네임스페이스">{selected.namespace ?? "-"}</Descriptions.Item>
              <Descriptions.Item label="클러스터">{selected.cluster_name}</Descriptions.Item>
              <Descriptions.Item label="시작 시각">
                {dayjs(selected.starts_at).format("YYYY-MM-DD HH:mm:ss")}
              </Descriptions.Item>
              <Descriptions.Item label="최초 수신">
                {dayjs(selected.first_received_at).format("YYYY-MM-DD HH:mm:ss")}
              </Descriptions.Item>
              <Descriptions.Item label="최종 수신">
                {dayjs(selected.last_received_at).format("YYYY-MM-DD HH:mm:ss")}
              </Descriptions.Item>
              <Descriptions.Item label="수신 횟수">{selected.receive_count}</Descriptions.Item>
              <Descriptions.Item label="확인">
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
                      확인 취소
                    </Button>
                  </Space>
                ) : (
                  <Button
                    size="small"
                    type="primary"
                    loading={ackMutation.isPending}
                    onClick={() => ackMutation.mutate()}
                  >
                    확인
                  </Button>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="담당자">
                <Select
                  allowClear
                  placeholder="담당자 지정"
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

            <Title level={5}>레이블</Title>
            <div style={{ marginBottom: 24 }}>
              {Object.entries(selected.labels).map(([key, value]) => (
                <Tag key={key} style={{ marginBottom: 4 }}>
                  {key}={value}
                </Tag>
              ))}
            </div>

            <Title level={5}>어노테이션</Title>
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
                  Prometheus에서 보기
                </a>
              )}
              {selected.grafana_url && (
                <a href={selected.grafana_url} target="_blank" rel="noreferrer">
                  Grafana에서 보기
                </a>
              )}
            </Space>

            <Title level={5} style={{ marginTop: 24 }}>
              댓글
            </Title>
            <List
              size="small"
              loading={commentsQuery.isLoading}
              dataSource={commentsQuery.data ?? []}
              locale={{ emptyText: <Empty description="댓글이 없습니다" /> }}
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
                              title="이 댓글을 삭제하시겠습니까?"
                              onConfirm={() => deleteCommentMutation.mutate(comment.id)}
                            >
                              <Button size="small" type="text" danger>
                                삭제
                              </Button>
                            </Popconfirm>,
                          ]
                        : []
                    }
                  >
                    <List.Item.Meta
                      title={
                        <Space>
                          <Text strong>{comment.user?.display_name ?? "(탈퇴한 사용자)"}</Text>
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
                placeholder="댓글을 입력하세요"
                value={commentText}
                onChange={(e) => setCommentText(e.target.value)}
              />
              <Button
                type="primary"
                loading={addCommentMutation.isPending}
                disabled={!commentText.trim()}
                onClick={() => addCommentMutation.mutate(commentText)}
              >
                등록
              </Button>
            </Space.Compact>

            <Title level={5}>알림 전송 이력</Title>
            <Table<AlertNotificationRecord>
              rowKey="id"
              size="small"
              loading={notificationsQuery.isLoading}
              dataSource={notificationsQuery.data ?? []}
              pagination={false}
              locale={{ emptyText: <Empty description="발송된 알림이 없습니다" /> }}
              columns={[
                { title: "채널", dataIndex: "channel_name", key: "channel_name" },
                { title: "트리거", dataIndex: "trigger", key: "trigger" },
                {
                  title: "상태",
                  dataIndex: "status",
                  key: "status",
                  render: (value: NotificationStatus) => (
                    <Tag color={NOTIFICATION_STATUS_COLOR[value]}>
                      {NOTIFICATION_STATUS_LABEL[value]}
                    </Tag>
                  ),
                },
                { title: "시도 횟수", dataIndex: "attempts", key: "attempts" },
                {
                  title: "발송 시각",
                  dataIndex: "delivered_at",
                  key: "delivered_at",
                  render: (value: string | null) =>
                    value ? dayjs(value).format("YYYY-MM-DD HH:mm:ss") : "-",
                },
                {
                  title: "오류",
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
