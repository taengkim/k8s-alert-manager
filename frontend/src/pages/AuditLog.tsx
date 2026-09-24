import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import dayjs, { type Dayjs } from "dayjs";
import { DatePicker, Empty, Popover, Select, Space, Table, Tag, Typography } from "antd";
import { getAuditLogs, type AuditLogEntry } from "../api/audit";
import { listUsers } from "../api/admin";

const { RangePicker } = DatePicker;
const { Text } = Typography;

// Action namespaces written across the backend (grep `action="..."` under
// app/ -- see app/services/audit.py's callers). Kept as an explicit list
// rather than derived from data so the filter always offers every known
// prefix, even one that hasn't fired yet in this deployment -- there's no
// enforcement tying this list to the backend, though, so a newly added
// action prefix still needs to be added here by hand.
const ACTION_PREFIXES: { value: string; label: string }[] = [
  { value: "alert.", label: "알럿" },
  { value: "auth.", label: "인증" },
  { value: "channel.", label: "채널" },
  { value: "cluster.", label: "클러스터" },
  { value: "history.", label: "이력" },
  { value: "report.", label: "리포트" },
  { value: "retention.", label: "보관 정책" },
  { value: "route.", label: "라우트" },
  { value: "rule.", label: "규칙" },
  { value: "rules.", label: "규칙 일괄 작업" },
  { value: "settings.", label: "설정" },
  { value: "share.", label: "공유" },
  { value: "silence.", label: "사일런스" },
  { value: "team.", label: "팀" },
  { value: "template.", label: "템플릿" },
  { value: "user.", label: "사용자" },
];

const PREFIX_COLOR: Record<string, string> = {
  rule: "blue",
  rules: "blue",
  silence: "purple",
  channel: "green",
  team: "orange",
  cluster: "red",
};

function actionTagColor(action: string): string {
  const prefix = action.split(".")[0];
  return PREFIX_COLOR[prefix] ?? "default";
}

interface AuditLogProps {
  /** Present for the team-settings "감사 로그" tab -- pins the query to
   * this one team and hides the (admin-only) team-wide affordances. Absent
   * for the admin "감사 로그" tab, which sees every team unscoped. */
  fixedTeamId?: number;
}

export default function AuditLog({ fixedTeamId }: AuditLogProps) {
  const isAdminView = fixedTeamId === undefined;

  const [action, setAction] = useState<string | undefined>(undefined);
  const [userId, setUserId] = useState<number | undefined>(undefined);
  const [range, setRange] = useState<[Dayjs | null, Dayjs | null] | null>(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(50);

  // The user filter needs the full user directory (GET /admin/users),
  // which only an admin may call -- a team owner has no equivalent here,
  // matching the brief's "사용자 Select(admin)" scoping.
  const usersQuery = useQuery({ queryKey: ["admin-users"], queryFn: listUsers, enabled: isAdminView });

  const fromTs = range?.[0]?.toISOString();
  const toTs = range?.[1]?.toISOString();

  const query = useQuery({
    queryKey: ["audit-logs", fixedTeamId, action, userId, fromTs, toTs, page, pageSize],
    queryFn: () =>
      getAuditLogs({ teamId: fixedTeamId, action, userId, fromTs, toTs, page, pageSize }),
  });

  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;

  const resetToFirstPage = <T,>(setter: (value: T) => void) => (value: T) => {
    setter(value);
    setPage(1);
  };

  const columns = [
    {
      title: "시각",
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: string) => dayjs(value).format("YYYY-MM-DD HH:mm:ss"),
    },
    {
      title: "사용자",
      dataIndex: "username",
      key: "username",
      width: 140,
      render: (value: string | null) => value ?? <Text type="secondary">시스템</Text>,
    },
    {
      title: "작업",
      dataIndex: "action",
      key: "action",
      width: 160,
      render: (value: string) => <Tag color={actionTagColor(value)}>{value}</Tag>,
    },
    {
      title: "대상",
      key: "object",
      render: (_: unknown, record: AuditLogEntry) => (
        <Space size={4}>
          <Text type="secondary">{record.object_type}</Text>
          <Text code>{record.object_ref}</Text>
        </Space>
      ),
    },
    {
      title: "상세",
      key: "detail",
      width: 80,
      render: (_: unknown, record: AuditLogEntry) =>
        record.detail ? (
          <Popover
            title="상세 정보"
            content={
              <pre style={{ margin: 0, maxWidth: 400, maxHeight: 300, overflow: "auto" }}>
                {JSON.stringify(record.detail, null, 2)}
              </pre>
            }
          >
            <a>보기</a>
          </Popover>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
  ];

  return (
    <div>
      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap", alignItems: "center" }}>
        <Select
          allowClear
          placeholder="작업 종류"
          style={{ minWidth: 160 }}
          options={ACTION_PREFIXES}
          value={action}
          onChange={resetToFirstPage(setAction)}
        />
        {isAdminView && (
          <Select
            allowClear
            showSearch
            placeholder="사용자"
            style={{ minWidth: 200 }}
            loading={usersQuery.isLoading}
            optionFilterProp="label"
            options={(usersQuery.data ?? []).map((u) => ({
              value: u.id,
              label: `${u.username} (${u.display_name})`,
            }))}
            value={userId}
            onChange={resetToFirstPage(setUserId)}
          />
        )}
        <RangePicker showTime value={range} onChange={resetToFirstPage(setRange)} />
      </div>

      <Table<AuditLogEntry>
        rowKey="id"
        loading={query.isLoading}
        dataSource={items}
        columns={columns}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          pageSizeOptions: [20, 50, 100, 200],
          onChange: (nextPage, nextPageSize) => {
            setPage(nextPage);
            setPageSize(nextPageSize);
          },
        }}
        locale={{ emptyText: <Empty description="감사 로그가 없습니다" /> }}
      />
    </div>
  );
}
