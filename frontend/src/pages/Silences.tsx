import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import { Alert, App, Button, Empty, Popconfirm, Segmented, Table, Tag, Tooltip, Typography } from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { ApiError } from "../api/client";
import { expireSilence, listSilences } from "../api/silences";
import type { SilenceOut, SilenceStatus } from "../api/silences";
import SilenceModal from "../components/SilenceModal";
import { useI18n } from "../i18n";

dayjs.extend(relativeTime);

const { Text } = Typography;

type StatusFilter = "all" | SilenceStatus;

const STATUS_TAG: Record<SilenceStatus, { color: string; label: string }> = {
  active: { color: "red", label: "active" },
  pending: { color: "blue", label: "pending" },
  expired: { color: "default", label: "expired" },
};

export default function Silences() {
  const { t } = useI18n();
  const { user } = useAuth();
  const { currentTeam } = useTeam();
  const { activeClusters, isLoading: clusterLoading } = useClusterFilter();
  const queryClient = useQueryClient();
  const { message } = App.useApp();

  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [modalOpen, setModalOpen] = useState(false);

  const isAdmin = !!user?.is_admin;
  const clusterIds = useMemo(() => activeClusters.map((c) => c.id), [activeClusters]);

  const query = useQuery({
    queryKey: ["silences", clusterIds],
    queryFn: () => listSilences(clusterIds),
    enabled: clusterIds.length > 0,
    refetchInterval: 30_000,
  });

  const expireMutation = useMutation({
    mutationFn: (record: SilenceOut) => expireSilence(record.id, record.cluster.id),
    onSuccess: () => {
      message.success(t("silences.expireSuccess"));
      queryClient.invalidateQueries({ queryKey: ["silences"] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("silences.expireError")}: ${err.detail}` : t("silences.expireError"),
      );
    },
  });

  const silences = useMemo(() => query.data?.silences ?? [], [query.data]);
  const filtered = useMemo(
    () => (statusFilter === "all" ? silences : silences.filter((s) => s.status === statusFilter)),
    [silences, statusFilter],
  );

  const canExpire = (record: SilenceOut): boolean => {
    if (!user) return false;
    if (user.is_admin) return true;
    if (!record.team) return false;
    return user.teams.some((t) => t.id === record.team!.id);
  };

  if (!currentTeam && !isAdmin) {
    return (
      <div>
        <h2>{t("silences.title")}</h2>
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
      width: 100,
      render: (statusValue: SilenceStatus) => {
        const tag = STATUS_TAG[statusValue] ?? STATUS_TAG.expired;
        return <Tag color={tag.color}>{tag.label}</Tag>;
      },
    },
    {
      title: t("silences.matchersColumn"),
      dataIndex: "matchers",
      key: "matchers",
      render: (matchers: SilenceOut["matchers"]) => (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {matchers.map((m, idx) => (
            <Text code key={`${m.name}-${idx}`}>
              {m.name}
              {m.isRegex ? "=~" : "="}
              {m.value}
            </Text>
          ))}
        </div>
      ),
    },
    {
      title: t("silences.durationColumn"),
      key: "duration",
      render: (_: unknown, record: SilenceOut) => (
        <Tooltip
          title={
            record.status === "expired"
              ? t("silences.expiredAgo", { time: dayjs(record.endsAt).fromNow() })
              : t("silences.expiresIn", { time: dayjs(record.endsAt).fromNow() })
          }
        >
          <span>
            {dayjs(record.startsAt).format("MM-DD HH:mm")} ~{" "}
            {dayjs(record.endsAt).format("MM-DD HH:mm")}
          </span>
        </Tooltip>
      ),
    },
    { title: t("common.description"), dataIndex: "comment", key: "comment", ellipsis: true },
    { title: t("silences.createdByColumn"), dataIndex: "createdBy", key: "createdBy", width: 120 },
    {
      title: t("common.cluster"),
      dataIndex: "cluster",
      key: "cluster",
      width: 120,
      render: (cluster: SilenceOut["cluster"]) => <Tag>{cluster.name}</Tag>,
    },
    {
      title: t("common.team"),
      dataIndex: "team",
      key: "team",
      width: 120,
      render: (team: SilenceOut["team"]) =>
        team ? <Tag color="blue">{team.slug}</Tag> : <Tag>{t("silences.externalTag")}</Tag>,
    },
    {
      title: "",
      key: "actions",
      width: 100,
      render: (_: unknown, record: SilenceOut) => {
        const allowed = canExpire(record) && record.status !== "expired";
        const button = (
          <Button
            size="small"
            danger
            disabled={!allowed}
            loading={expireMutation.isPending && expireMutation.variables?.id === record.id}
          >
            {t("silences.expireButton")}
          </Button>
        );
        if (!allowed) {
          return (
            <Tooltip title={record.status === "expired" ? t("silences.alreadyExpired") : t("common.noPermission")}>
              <span>{button}</span>
            </Tooltip>
          );
        }
        return (
          <Popconfirm
            title={t("silences.expireConfirm")}
            onConfirm={() => expireMutation.mutate(record)}
          >
            {button}
          </Popconfirm>
        );
      },
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
          {t("silences.title")}{" "}
          <Text type="secondary" style={{ fontSize: 14, fontWeight: "normal" }}>
            ({t("alerts.count", { count: filtered.length })})
          </Text>
        </h2>
        <Tooltip title={currentTeam ? undefined : t("common.noTeamAssigned")}>
          <Button type="primary" disabled={!currentTeam} onClick={() => setModalOpen(true)}>
            {t("silences.createButton")}
          </Button>
        </Tooltip>
      </div>

      <Segmented
        value={statusFilter}
        onChange={(value) => setStatusFilter(value as StatusFilter)}
        options={[
          { label: t("common.all"), value: "all" },
          { label: t("common.active"), value: "active" },
          { label: t("common.pending"), value: "pending" },
          { label: t("common.expired"), value: "expired" },
        ]}
        style={{ marginBottom: 16 }}
      />

      <Table<SilenceOut>
        rowKey={(record) => `${record.cluster.id}-${record.id}`}
        loading={query.isLoading || clusterLoading}
        dataSource={filtered}
        columns={columns}
        pagination={{ pageSize: 20 }}
        locale={{ emptyText: <Empty description={t("silences.empty")} /> }}
      />

      {currentTeam && (
        <SilenceModal
          open={modalOpen}
          onClose={() => setModalOpen(false)}
          clusters={activeClusters}
          team={currentTeam}
        />
      )}
    </div>
  );
}
