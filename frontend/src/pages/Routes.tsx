import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Empty, Popconfirm, Space, Switch, Table, Tag, Typography } from "antd";
import { useNavigate } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { deleteRoute, listRoutes, updateRoute } from "../api/routes";
import type { RouteOut, RouteWriteInput } from "../api/routes";
import TestAlertModal from "../components/TestAlertModal";

const { Text } = Typography;

const SEVERITY_TAG_COLOR: Record<string, string> = {
  critical: "red",
  warning: "orange",
  info: "blue",
  none: "default",
};

function toWriteInput(route: RouteOut, overrides: Partial<RouteWriteInput> = {}): RouteWriteInput {
  return {
    name: route.name,
    description: route.description ?? undefined,
    action: route.action,
    enabled: route.enabled,
    notify_on_firing: route.notify_on_firing,
    notify_on_resolved: route.notify_on_resolved,
    include_shared: route.include_shared,
    severities: route.severities ?? undefined,
    namespaces_include: route.namespaces_include ?? undefined,
    namespaces_exclude: route.namespaces_exclude ?? undefined,
    clusters: route.clusters ?? undefined,
    channel_ids: route.channel_ids,
    escalation_enabled: route.escalation_enabled,
    escalation_after_minutes: route.escalation_after_minutes ?? undefined,
    escalation_channel_ids: route.escalation_channel_ids,
    renotify_interval_minutes: route.renotify_interval_minutes ?? undefined,
    matchers: route.matchers.map((m) => ({
      kind: m.kind,
      target: m.target,
      key: m.key ?? undefined,
      pattern: m.pattern,
    })),
    ...overrides,
  };
}

export default function Routes() {
  const navigate = useNavigate();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const { currentTeam } = useTeam();

  const isOwner = useMemo(() => {
    if (!user || !currentTeam) return false;
    if (user.is_admin) return true;
    const membership = user.teams.find((t) => t.id === currentTeam.id);
    return membership?.role === "owner";
  }, [user, currentTeam]);

  const teamId = currentTeam?.id;
  const [testAlertModalOpen, setTestAlertModalOpen] = useState(false);

  const routesQuery = useQuery({
    queryKey: ["routes", teamId],
    queryFn: () => listRoutes(teamId!),
    enabled: !!teamId,
  });

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const clusterNameById = useMemo(() => {
    const map = new Map<number, string>();
    for (const c of clustersQuery.data ?? []) map.set(c.id, c.display_name);
    return map;
  }, [clustersQuery.data]);

  const toggleMutation = useMutation({
    mutationFn: ({ route, enabled }: { route: RouteOut; enabled: boolean }) =>
      updateRoute(route.id, toWriteInput(route, { enabled })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["routes", teamId] }),
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `변경에 실패했습니다: ${err.detail}` : "변경에 실패했습니다",
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteRoute(id),
    onSuccess: () => {
      message.success("규칙이 삭제되었습니다");
      queryClient.invalidateQueries({ queryKey: ["routes", teamId] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  if (!currentTeam) {
    return (
      <div>
        <h2>라우팅 규칙</h2>
        <Alert
          type="info"
          showIcon
          message="소속된 팀이 없습니다"
          description="관리자에게 팀 추가를 요청하세요."
        />
      </div>
    );
  }

  const routes = routesQuery.data ?? [];

  const columns = [
    { title: "이름", dataIndex: "name", key: "name" },
    {
      title: "액션",
      dataIndex: "action",
      key: "action",
      render: (action: string) => (
        <Tag color={action === "suppress" ? "red" : "green"}>
          {action === "suppress" ? "차단" : "알림"}
        </Tag>
      ),
    },
    {
      title: "심각도",
      key: "severities",
      render: (_: unknown, route: RouteOut) =>
        route.severities && route.severities.length > 0 ? (
          <Space size={4} wrap>
            {route.severities.map((s) => (
              <Tag key={s} color={SEVERITY_TAG_COLOR[s] ?? "default"}>
                {s}
              </Tag>
            ))}
          </Space>
        ) : (
          <Text type="secondary">전체</Text>
        ),
    },
    {
      title: "네임스페이스",
      key: "namespaces",
      render: (_: unknown, route: RouteOut) => {
        const chips: ReactNode[] = [];
        for (const ns of route.namespaces_include ?? []) {
          chips.push(
            <Tag key={`i-${ns}`} color="blue">
              {ns}
            </Tag>,
          );
        }
        for (const ns of route.namespaces_exclude ?? []) {
          chips.push(
            <Tag key={`e-${ns}`} color="red">
              !{ns}
            </Tag>,
          );
        }
        return chips.length > 0 ? <Space size={4} wrap>{chips}</Space> : <Text type="secondary">전체</Text>;
      },
    },
    {
      title: "클러스터",
      key: "clusters",
      render: (_: unknown, route: RouteOut) =>
        route.clusters && route.clusters.length > 0 ? (
          <Space size={4} wrap>
            {route.clusters.map((id) => (
              <Tag key={id}>{clusterNameById.get(id) ?? id}</Tag>
            ))}
          </Space>
        ) : (
          <Text type="secondary">전체</Text>
        ),
    },
    {
      title: "채널",
      dataIndex: "channel_ids",
      key: "channel_ids",
      render: (ids: number[]) =>
        ids.length > 0 ? `${ids.length}개` : <Text type="secondary">-</Text>,
    },
    {
      title: "활성화",
      dataIndex: "enabled",
      key: "enabled",
      render: (enabled: boolean, route: RouteOut) => (
        <Switch
          checked={enabled}
          disabled={!isOwner}
          loading={toggleMutation.isPending && toggleMutation.variables?.route.id === route.id}
          onChange={(checked) => toggleMutation.mutate({ route, enabled: checked })}
        />
      ),
    },
    {
      title: "",
      key: "actions",
      render: (_: unknown, route: RouteOut) =>
        isOwner ? (
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="small" onClick={() => navigate(`/routes/${route.id}/edit`)}>
              수정
            </Button>
            <Popconfirm
              title="이 규칙을 삭제하시겠습니까?"
              onConfirm={() => deleteMutation.mutate(route.id)}
            >
              <Button size="small" danger loading={deleteMutation.isPending}>
                삭제
              </Button>
            </Popconfirm>
          </div>
        ) : null,
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
        <h2 style={{ margin: 0 }}>라우팅 규칙 — {currentTeam.name}</h2>
        <Space>
          <Button onClick={() => setTestAlertModalOpen(true)}>테스트 알럿 발사</Button>
          {isOwner && (
            <Button type="primary" onClick={() => navigate("/routes/new")}>
              규칙 생성
            </Button>
          )}
        </Space>
      </div>

      <Table<RouteOut>
        rowKey="id"
        loading={routesQuery.isLoading}
        dataSource={routes}
        columns={columns}
        pagination={false}
        locale={{ emptyText: <Empty description="규칙이 없습니다" /> }}
      />

      <TestAlertModal
        open={testAlertModalOpen}
        onClose={() => setTestAlertModalOpen(false)}
        teamId={currentTeam.id}
      />
    </div>
  );
}
