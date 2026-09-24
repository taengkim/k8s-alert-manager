import { useMemo, useState } from "react";
import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  App,
  Badge,
  Button,
  Empty,
  Popconfirm,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { useNavigate } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { ApiError } from "../api/client";
import { deleteRule, downloadRulesExport, listRules } from "../api/rules";
import type { RuleOut } from "../api/rules";
import type { Cluster } from "../api/types";
import RuleImportModal from "../components/RuleImportModal";
import { severityTagStyle } from "../theme";
import { useI18n } from "../i18n";

interface RuleRow extends RuleOut {
  cluster: Cluster;
}

const { Text } = Typography;

const HEALTH_BADGE: Record<string, { status: "success" | "error" | "default"; text: string }> = {
  ok: { status: "success", text: "ok" },
  err: { status: "error", text: "err" },
  unknown: { status: "default", text: "unknown" },
};

export default function Rules() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const { currentTeam, teams } = useTeam();
  const { activeClusters, isLoading: clusterLoading } = useClusterFilter();

  const teamId = currentTeam?.id;
  const [selectedRowKeys, setSelectedRowKeys] = useState<string[]>([]);
  const [importModalOpen, setImportModalOpen] = useState(false);

  const isOwner = useMemo(() => {
    if (!user || !currentTeam) return false;
    if (user.is_admin) return true;
    const membership = user.teams.find((t) => t.id === currentTeam.id);
    return membership?.role === "owner";
  }, [user, currentTeam]);

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
      message.success(t("rules.deleteSuccess"));
      queryClient.invalidateQueries({ queryKey: ["rules", teamId, rule.cluster.id] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  // With a row selection, export just those rules (grouped by cluster, since
  // the export API is per-cluster); with none, export every currently active
  // cluster's rules -- one file download per cluster either way.
  const exportMutation = useMutation({
    mutationFn: async () => {
      if (!teamId) return;
      if (selectedRowKeys.length > 0) {
        const selected = rules.filter((r) =>
          selectedRowKeys.includes(`${r.cluster.id}-${r.slug}`),
        );
        const slugsByCluster = new Map<number, string[]>();
        for (const rule of selected) {
          const slugs = slugsByCluster.get(rule.cluster.id) ?? [];
          slugs.push(rule.slug);
          slugsByCluster.set(rule.cluster.id, slugs);
        }
        for (const [clusterId, slugs] of slugsByCluster) {
          await downloadRulesExport(teamId, clusterId, { slugs });
        }
      } else {
        for (const cluster of activeClusters) {
          await downloadRulesExport(teamId, cluster.id);
        }
      }
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.exportError")}: ${err.detail}` : t("common.exportError"),
      );
    },
  });

  if (teams.length === 0 || !currentTeam) {
    return (
      <div>
        <h2>{t("rules.title")}</h2>
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
    { title: t("alerts.alertName"), dataIndex: "alert_name", key: "alert_name" },
    { title: t("rules.slugColumn"), dataIndex: "slug", key: "slug" },
    {
      title: t("common.cluster"),
      key: "cluster",
      width: 140,
      render: (_: unknown, record: RuleRow) => <Tag>{record.cluster.display_name}</Tag>,
    },
    {
      title: t("common.severity"),
      dataIndex: "severity",
      key: "severity",
      render: (value: string) => <Tag style={severityTagStyle(value)}>{value}</Tag>,
    },
    {
      title: t("rules.exprColumn"),
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
      title: t("common.status"),
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
            {t("common.edit")}
          </Button>
          <Popconfirm
            title={t("rules.deleteConfirm")}
            onConfirm={() => deleteMutation.mutate(record)}
          >
            <Button
              size="small"
              danger
              loading={deleteMutation.isPending && deleteMutation.variables?.slug === record.slug}
            >
              {t("common.delete")}
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
          {t("rules.title")}{" "}
          <Text type="secondary" style={{ fontSize: 14, fontWeight: "normal" }}>
            ({t("alerts.count", { count: rules.length })})
          </Text>
        </h2>
        <Space>
          <Button loading={exportMutation.isPending} onClick={() => exportMutation.mutate()}>
            {t("common.export")}
            {selectedRowKeys.length > 0
              ? t("rules.selectedSuffix", { count: selectedRowKeys.length })
              : ""}
          </Button>
          {isOwner && <Button onClick={() => setImportModalOpen(true)}>{t("common.import")}</Button>}
          <Button type="primary" onClick={() => navigate("/rules/new")}>
            {t("rules.createButton")}
          </Button>
        </Space>
      </div>

      {warnings.map((warning) => (
        <Alert key={warning} type="warning" showIcon style={{ marginBottom: 12 }} message={warning} />
      ))}

      <Table<RuleRow>
        rowKey={(record) => `${record.cluster.id}-${record.slug}`}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys as string[]),
        }}
        loading={isLoading}
        dataSource={rules}
        columns={columns}
        pagination={{ pageSize: 20 }}
        locale={{ emptyText: <Empty description={t("rules.empty")} /> }}
      />

      <RuleImportModal
        open={importModalOpen}
        onClose={() => setImportModalOpen(false)}
        teamId={teamId!}
      />
    </div>
  );
}
