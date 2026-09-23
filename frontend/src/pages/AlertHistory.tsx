import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import dayjs, { type Dayjs } from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import {
  Alert,
  Badge,
  DatePicker,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Segmented,
  Select,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import {
  getAlertHistory,
  getAlertHistoryDetail,
  getAlertHistoryNotifications,
} from "../api/history";
import type {
  AlertEventStatus,
  AlertEventSummary,
  AlertNotificationRecord,
  NotificationStatus,
} from "../api/history";

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
  const { user } = useAuth();
  const { currentTeam, teams } = useTeam();
  const isAdmin = !!user?.is_admin;

  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [severity, setSeverity] = useState<string[]>([]);
  const [namespace, setNamespace] = useState<string | undefined>(undefined);
  const [search, setSearch] = useState("");
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const teamId = currentTeam?.id;
  const noTeamSelected = !isAdmin && teams.length === 0;
  const fromTs = range?.[0] ? range[0].toISOString() : undefined;
  const toTs = range?.[1] ? range[1].toISOString() : undefined;

  const query = useQuery({
    queryKey: [
      "alert-history",
      teamId,
      statusFilter,
      severity,
      namespace,
      search,
      fromTs,
      toTs,
      page,
      pageSize,
    ],
    queryFn: () =>
      getAlertHistory({
        teamId,
        status: statusFilter === "all" ? undefined : statusFilter,
        severity: severity.length > 0 ? severity : undefined,
        namespace,
        search: search || undefined,
        fromTs,
        toTs,
        page,
        pageSize,
      }),
    enabled: isAdmin || !!teamId,
  });

  const detailQuery = useQuery({
    queryKey: ["alert-history-detail", selectedId],
    queryFn: () => getAlertHistoryDetail(selectedId as number),
    enabled: selectedId !== null,
  });

  const notificationsQuery = useQuery({
    queryKey: ["alert-history-notifications", selectedId],
    queryFn: () => getAlertHistoryNotifications(selectedId as number),
    enabled: selectedId !== null,
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
    { title: "알럿명", dataIndex: "alertname", key: "alertname" },
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

  const selected = selectedId !== null ? detailQuery.data : undefined;

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
      </div>

      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
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
        title={selected?.alertname}
        open={selectedId !== null}
        onClose={() => setSelectedId(null)}
        width={480}
        loading={detailQuery.isLoading}
      >
        {selected && (
          <>
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

            {selected.generator_url && (
              <a href={selected.generator_url} target="_blank" rel="noreferrer">
                Prometheus에서 보기
              </a>
            )}

            <Title level={5} style={{ marginTop: 24 }}>
              알림 전송 이력
            </Title>
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
