import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import dayjs, { type Dayjs } from "dayjs";
import { DatePicker, Empty, Popover, Select, Space, Table, Tag, Typography } from "antd";
import { getAuditLogs, type AuditLogEntry } from "../api/audit";
import { listUsers } from "../api/admin";
import { useI18n } from "../i18n";

const { RangePicker } = DatePicker;
const { Text } = Typography;

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
  const { t } = useI18n();
  const isAdminView = fixedTeamId === undefined;

  // Action namespaces written across the backend (grep `action="..."` under
  // app/ -- see app/services/audit.py's callers). Kept as an explicit list
  // rather than derived from data so the filter always offers every known
  // prefix, even one that hasn't fired yet in this deployment -- there's no
  // enforcement tying this list to the backend, though, so a newly added
  // action prefix still needs to be added here by hand.
  const ACTION_PREFIXES: { value: string; label: string }[] = [
    { value: "alert.", label: t("alerts.title") },
    { value: "auth.", label: t("audit.actionAuth") },
    { value: "channel.", label: t("common.channel") },
    { value: "cluster.", label: t("common.cluster") },
    { value: "history.", label: t("audit.actionHistory") },
    { value: "report.", label: t("team.reportsTab") },
    { value: "retention.", label: t("audit.actionRetention") },
    { value: "route.", label: t("audit.actionRoute") },
    { value: "rule.", label: t("audit.actionRule") },
    { value: "rules.", label: t("audit.actionRulesBulk") },
    { value: "settings.", label: t("audit.actionSettings") },
    { value: "share.", label: t("shares.title") },
    { value: "silence.", label: t("silences.title") },
    { value: "team.", label: t("common.team") },
    { value: "template.", label: t("templates.title") },
    { value: "user.", label: t("team.userLabel") },
  ];

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
      title: t("audit.timeColumn"),
      dataIndex: "created_at",
      key: "created_at",
      width: 170,
      render: (value: string) => dayjs(value).format("YYYY-MM-DD HH:mm:ss"),
    },
    {
      title: t("team.userLabel"),
      dataIndex: "username",
      key: "username",
      width: 140,
      render: (value: string | null) => value ?? <Text type="secondary">{t("audit.systemUser")}</Text>,
    },
    {
      title: t("audit.actionColumn"),
      dataIndex: "action",
      key: "action",
      width: 160,
      render: (value: string) => <Tag color={actionTagColor(value)}>{value}</Tag>,
    },
    {
      title: t("audit.objectColumn"),
      key: "object",
      render: (_: unknown, record: AuditLogEntry) => (
        <Space size={4}>
          <Text type="secondary">{record.object_type}</Text>
          <Text code>{record.object_ref}</Text>
        </Space>
      ),
    },
    {
      title: t("audit.detailColumn"),
      key: "detail",
      width: 80,
      render: (_: unknown, record: AuditLogEntry) =>
        record.detail ? (
          <Popover
            title={t("audit.detailPopoverTitle")}
            content={
              <pre style={{ margin: 0, maxWidth: 400, maxHeight: 300, overflow: "auto" }}>
                {JSON.stringify(record.detail, null, 2)}
              </pre>
            }
          >
            <a>{t("common.view")}</a>
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
          placeholder={t("audit.actionTypePlaceholder")}
          style={{ minWidth: 160 }}
          options={ACTION_PREFIXES}
          value={action}
          onChange={resetToFirstPage(setAction)}
        />
        {isAdminView && (
          <Select
            allowClear
            showSearch
            placeholder={t("team.userLabel")}
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
        locale={{ emptyText: <Empty description={t("audit.empty")} /> }}
      />
    </div>
  );
}
