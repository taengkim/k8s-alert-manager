import { useMemo } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Badge, Button, Empty, Popconfirm, Table, Tag, Tooltip, Typography } from "antd";
import { useNavigate } from "react-router";
import { useTeam } from "../auth/TeamContext";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { ApiError } from "../api/client";
import { deleteRule, listRules } from "../api/rules";
import type { RuleOut } from "../api/rules";
import type { Cluster } from "../api/types";

interface RuleRow extends RuleOut {
  cluster: Cluster;
}

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
  const { activeClusters, isLoading: clusterLoading } = useClusterFilter();

  const teamId = currentTeam?.id;

  // A team's rules live per-cluster (each is a k8s PrometheusRule on that
  // specific cluster's API server), so the ClusterFilter selection is
  // queried per-cluster in parallel and merged here -- rather than one
  // "all clusters" endpoint, which would need every cluster reachable to
  // answer at all.
  const ruleQueries = useQueries({
    queries: activeClusters.map((cluster) => ({
      queryKey: ["rules", teamId, cluster.id],
      queryFn: () => listRules(teamId!, cluster.id),
      enabled: !!teamId,
    })),
  });

  const rules: RuleRow[] = useMemo(
    () =>
      ruleQueries.flatMap((q, idx) =>
        (q.data?.rules ?? []).map((rule) => ({ ...rule, cluster: activeClusters[idx] })),
      ),
    [ruleQueries, activeClusters],
  );
  const warnings = useMemo(
    () =>
      ruleQueries
        .map((q, idx) =>
          q.data?.warning ? `${activeClusters[idx].display_name}: ${q.data.warning}` : null,
        )
        .filter((w): w is string => w !== null),
    [ruleQueries, activeClusters],
  );
  const isLoading = clusterLoading || ruleQueries.some((q) => q.isLoading);

  const deleteMutation = useMutation({
    mutationFn: (rule: RuleRow) => deleteRule(teamId!, rule.cluster.id, rule.slug),
    onSuccess: (_data, rule) => {
      message.success("룰이 삭제되었습니다");
      queryClient.invalidateQueries({ queryKey: ["rules", teamId, rule.cluster.id] });
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

  const columns = [
    { title: "알럿명", dataIndex: "alert_name", key: "alert_name" },
    { title: "슬러그", dataIndex: "slug", key: "slug" },
    {
      title: "클러스터",
      key: "cluster",
      width: 140,
      render: (_: unknown, record: RuleRow) => <Tag>{record.cluster.display_name}</Tag>,
    },
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
      render: (_: unknown, record: RuleRow) => (
        <div style={{ display: "flex", gap: 8 }}>
          <Button
            size="small"
            onClick={() => navigate(`/rules/${record.slug}/edit?cluster=${record.cluster.id}`)}
          >
            수정
          </Button>
          <Popconfirm
            title="이 룰을 삭제하시겠습니까?"
            onConfirm={() => deleteMutation.mutate(record)}
          >
            <Button
              size="small"
              danger
              loading={deleteMutation.isPending && deleteMutation.variables?.slug === record.slug}
            >
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

      {warnings.map((warning) => (
        <Alert key={warning} type="warning" showIcon style={{ marginBottom: 12 }} message={warning} />
      ))}

      <Table<RuleRow>
        rowKey={(record) => `${record.cluster.id}-${record.slug}`}
        loading={isLoading}
        dataSource={rules}
        columns={columns}
        pagination={{ pageSize: 20 }}
        locale={{ emptyText: <Empty description="룰이 없습니다" /> }}
      />
    </div>
  );
}
