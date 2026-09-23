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

dayjs.extend(relativeTime);

const { Text } = Typography;

type StatusFilter = "all" | SilenceStatus;

const STATUS_TAG: Record<SilenceStatus, { color: string; label: string }> = {
  active: { color: "red", label: "active" },
  pending: { color: "blue", label: "pending" },
  expired: { color: "default", label: "expired" },
};

export default function Silences() {
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
      message.success("사일런스가 만료되었습니다");
      queryClient.invalidateQueries({ queryKey: ["silences"] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `만료에 실패했습니다: ${err.detail}` : "만료에 실패했습니다",
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
        <h2>사일런스</h2>
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
      width: 100,
      render: (statusValue: SilenceStatus) => {
        const tag = STATUS_TAG[statusValue] ?? STATUS_TAG.expired;
        return <Tag color={tag.color}>{tag.label}</Tag>;
      },
    },
    {
      title: "Matchers",
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
      title: "기간",
      key: "duration",
      render: (_: unknown, record: SilenceOut) => (
        <Tooltip
          title={
            record.status === "expired"
              ? `${dayjs(record.endsAt).fromNow()} 만료됨`
              : `${dayjs(record.endsAt).fromNow()} 만료 예정`
          }
        >
          <span>
            {dayjs(record.startsAt).format("MM-DD HH:mm")} ~{" "}
            {dayjs(record.endsAt).format("MM-DD HH:mm")}
          </span>
        </Tooltip>
      ),
    },
    { title: "설명", dataIndex: "comment", key: "comment", ellipsis: true },
    { title: "생성자", dataIndex: "createdBy", key: "createdBy", width: 120 },
    {
      title: "클러스터",
      dataIndex: "cluster",
      key: "cluster",
      width: 120,
      render: (cluster: SilenceOut["cluster"]) => <Tag>{cluster.name}</Tag>,
    },
    {
      title: "팀",
      dataIndex: "team",
      key: "team",
      width: 120,
      render: (team: SilenceOut["team"]) =>
        team ? <Tag color="blue">{team.slug}</Tag> : <Tag>외부</Tag>,
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
            만료
          </Button>
        );
        if (!allowed) {
          return (
            <Tooltip title={record.status === "expired" ? "이미 만료됨" : "권한이 없습니다"}>
              <span>{button}</span>
            </Tooltip>
          );
        }
        return (
          <Popconfirm
            title="이 사일런스를 만료시키겠습니까?"
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
          사일런스{" "}
          <Text type="secondary" style={{ fontSize: 14, fontWeight: "normal" }}>
            ({filtered.length}건)
          </Text>
        </h2>
        <Tooltip title={currentTeam ? undefined : "소속된 팀이 없습니다"}>
          <Button type="primary" disabled={!currentTeam} onClick={() => setModalOpen(true)}>
            사일런스 생성
          </Button>
        </Tooltip>
      </div>

      <Segmented
        value={statusFilter}
        onChange={(value) => setStatusFilter(value as StatusFilter)}
        options={[
          { label: "전체", value: "all" },
          { label: "활성", value: "active" },
          { label: "대기", value: "pending" },
          { label: "만료", value: "expired" },
        ]}
        style={{ marginBottom: 16 }}
      />

      <Table<SilenceOut>
        rowKey={(record) => `${record.cluster.id}-${record.id}`}
        loading={query.isLoading || clusterLoading}
        dataSource={filtered}
        columns={columns}
        pagination={{ pageSize: 20 }}
        locale={{ emptyText: <Empty description="사일런스가 없습니다" /> }}
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
