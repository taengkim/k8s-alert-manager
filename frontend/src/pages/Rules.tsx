import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Badge, Button, Empty, Popconfirm, Table, Tag, Tooltip, Typography } from "antd";
import { useNavigate } from "react-router";
import { useTeam } from "../auth/TeamContext";
import { useDefaultCluster } from "../api/useDefaultCluster";
import { ApiError } from "../api/client";
import { deleteRule, listRules } from "../api/rules";
import type { RuleOut } from "../api/rules";

const { Text } = Typography;

const SEVERITY_TAG_COLOR: Record<string, string> = {
  critical: "red",
  warning: "orange",
  info: "blue",
};

const HEALTH_BADGE: Record<string, { status: "success" | "error" | "default"; text: string }> = {
  ok: { status: "success", text: "ok" },
  err: { status: "error", text: "err" },
  unknown: { status: "default", text: "unknown" },
};

export default function Rules() {
  const navigate = useNavigate();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { currentTeam, teams } = useTeam();
  const { cluster, isLoading: clusterLoading } = useDefaultCluster();

  const teamId = currentTeam?.id;
  const clusterId = cluster?.id;

  const query = useQuery({
    queryKey: ["rules", teamId, clusterId],
    queryFn: () => listRules(teamId!, clusterId!),
    enabled: !!teamId && !!clusterId,
  });

  const deleteMutation = useMutation({
    mutationFn: (slug: string) => deleteRule(teamId!, clusterId!, slug),
    onSuccess: () => {
      message.success("룰이 삭제되었습니다");
      queryClient.invalidateQueries({ queryKey: ["rules", teamId, clusterId] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  if (teams.length === 0 || !currentTeam) {
    return (
      <div>
        <h2>룰</h2>
        <Alert
          type="info"
          showIcon
          message="소속된 팀이 없습니다"
          description="관리자에게 팀 추가를 요청하세요."
        />
      </div>
    );
  }

  const rules = query.data?.rules ?? [];

  const columns = [
    { title: "알럿명", dataIndex: "alert_name", key: "alert_name" },
    { title: "슬러그", dataIndex: "slug", key: "slug" },
    {
      title: "심각도",
      dataIndex: "severity",
      key: "severity",
      render: (value: string) => <Tag color={SEVERITY_TAG_COLOR[value] ?? "default"}>{value}</Tag>,
    },
    {
      title: "표현식",
      dataIndex: "expr",
      key: "expr",
      ellipsis: true,
      render: (expr: string) => (
        <Tooltip title={expr}>
          <span style={{ fontFamily: "monospace" }}>{expr}</span>
        </Tooltip>
      ),
    },
    {
      title: "for",
      dataIndex: "for",
      key: "for",
      width: 80,
      render: (value: string | null) => value ?? <Text type="secondary">-</Text>,
    },
    {
      title: "상태",
      dataIndex: "health",
      key: "health",
      width: 100,
      render: (health: string) => {
        const badge = HEALTH_BADGE[health] ?? HEALTH_BADGE.unknown;
        return <Badge status={badge.status} text={badge.text} />;
      },
    },
    {
      title: "",
      key: "actions",
      width: 160,
      render: (_: unknown, record: RuleOut) => (
        <div style={{ display: "flex", gap: 8 }}>
          <Button size="small" onClick={() => navigate(`/rules/${record.slug}/edit`)}>
            수정
          </Button>
          <Popconfirm
            title="이 룰을 삭제하시겠습니까?"
            onConfirm={() => deleteMutation.mutate(record.slug)}
          >
            <Button size="small" danger loading={deleteMutation.isPending}>
              삭제
            </Button>
          </Popconfirm>
        </div>
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
          룰{" "}
          <Text type="secondary" style={{ fontSize: 14, fontWeight: "normal" }}>
            ({rules.length}건)
          </Text>
        </h2>
        <Button type="primary" onClick={() => navigate("/rules/new")}>
          룰 생성
        </Button>
      </div>

      {query.data?.warning && (
        <Alert type="warning" showIcon style={{ marginBottom: 12 }} message={query.data.warning} />
      )}

      <Table<RuleOut>
        rowKey="slug"
        loading={query.isLoading || clusterLoading}
        dataSource={rules}
        columns={columns}
        pagination={{ pageSize: 20 }}
        locale={{ emptyText: <Empty description="룰이 없습니다" /> }}
      />
    </div>
  );
}
