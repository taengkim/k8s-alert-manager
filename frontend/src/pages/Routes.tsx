import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Empty, Popconfirm, Space, Switch, Table, Tag, Typography } from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { deleteRoute, listRoutes, updateRoute } from "../api/routes";
import type { RouteOut, RouteWriteInput } from "../api/routes";
import TestAlertModal from "../components/TestAlertModal";
import RouteEditorModal from "../components/RouteEditorModal";
import { severityTagStyle } from "../theme";
import { useI18n } from "../i18n";

const { Text } = Typography;

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
    template_id: route.template_id ?? undefined,
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
  const { t } = useI18n();
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
  const [editorRouteId, setEditorRouteId] = useState<"new" | number | null>(null);

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
        err instanceof ApiError ? `${t("common.updateError")}: ${err.detail}` : t("common.updateError"),
      );
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteRoute(id),
    onSuccess: () => {
      message.success(t("routes.deleteSuccess"));
      queryClient.invalidateQueries({ queryKey: ["routes", teamId] });
    },
    onError: (err) => {
      message.error(
        err instanceof ApiError ? `${t("common.deleteError")}: ${err.detail}` : t("common.deleteError"),
      );
    },
  });

  if (!currentTeam) {
    return (
      <div>
        <h2>{t("routes.title")}</h2>
        <Alert
          type="info"
          showIcon
          message={t("common.noTeamAssigned")}
          description={t("common.requestTeamAssignment")}
        />
      </div>
    );
  }

  const routes = routesQuery.data ?? [];

  const columns = [
    { title: t("common.name"), dataIndex: "name", key: "name" },
    {
      title: t("ruleImport.actionColumn"),
      dataIndex: "action",
      key: "action",
      render: (action: string) => (
        <Tag color={action === "suppress" ? "red" : "green"}>
          {action === "suppress" ? t("testAlert.actionSuppress") : t("testAlert.actionNotify")}
        </Tag>
      ),
    },
    {
      title: t("common.severity"),
      key: "severities",
      render: (_: unknown, route: RouteOut) =>
        route.severities && route.severities.length > 0 ? (
          <Space size={4} wrap>
            {route.severities.map((s) => (
              <Tag key={s} style={severityTagStyle(s)}>
                {s}
              </Tag>
            ))}
          </Space>
        ) : (
          <Text type="secondary">{t("common.all")}</Text>
        ),
    },
    {
      title: t("common.namespace"),
      key: "namespaces",
      render: (_: unknown, route: RouteOut) => {
        const chips: ReactNode[] = [];
        for (const ns of route.namespaces_include ?? []) {
          chips.push(
            <Tag key={`i-${ns}`} color="blue" className="kam-mono">
              {ns}
            </Tag>,
          );
        }
        for (const ns of route.namespaces_exclude ?? []) {
          chips.push(
            <Tag key={`e-${ns}`} color="red" className="kam-mono">
              !{ns}
            </Tag>,
          );
        }
        return chips.length > 0 ? <Space size={4} wrap>{chips}</Space> : <Text type="secondary">{t("common.all")}</Text>;
      },
    },
    {
      title: t("common.cluster"),
      key: "clusters",
      render: (_: unknown, route: RouteOut) =>
        route.clusters && route.clusters.length > 0 ? (
          <Space size={4} wrap>
            {route.clusters.map((id) => (
              <Tag key={id}>{clusterNameById.get(id) ?? id}</Tag>
            ))}
          </Space>
        ) : (
          <Text type="secondary">{t("common.all")}</Text>
        ),
    },
    {
      title: t("common.channel"),
      dataIndex: "channel_ids",
      key: "channel_ids",
      render: (ids: number[]) =>
        ids.length > 0 ? t("routes.channelCount", { count: ids.length }) : <Text type="secondary">-</Text>,
    },
    {
      title: t("common.enabled"),
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
            <Button size="small" onClick={() => setEditorRouteId(route.id)}>
              {t("common.edit")}
            </Button>
            <Popconfirm
              title={t("routes.deleteConfirm")}
              onConfirm={() => deleteMutation.mutate(route.id)}
            >
              <Button size="small" danger loading={deleteMutation.isPending}>
                {t("common.delete")}
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
        <h2 style={{ margin: 0 }}>{t("routes.titleWithTeam", { team: currentTeam.name })}</h2>
        <Space>
          <Button onClick={() => setTestAlertModalOpen(true)}>{t("testAlert.modalTitle")}</Button>
          {isOwner && (
            <Button type="primary" onClick={() => setEditorRouteId("new")}>
              {t("routes.createButton")}
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
        locale={{ emptyText: <Empty description={t("routes.empty")} /> }}
      />

      <TestAlertModal
        open={testAlertModalOpen}
        onClose={() => setTestAlertModalOpen(false)}
        teamId={currentTeam.id}
      />

      <RouteEditorModal
        open={editorRouteId !== null}
        onClose={() => setEditorRouteId(null)}
        routeId={editorRouteId === "new" ? null : editorRouteId}
      />
    </div>
  );
}
