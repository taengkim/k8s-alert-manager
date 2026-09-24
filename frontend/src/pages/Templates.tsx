import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Empty, Popconfirm, Segmented, Space, Table, Tag, Typography } from "antd";
import { useNavigate } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import { deleteTemplate, listTemplates } from "../api/templates";
import type { MessageTemplate } from "../api/templates";
import { useI18n } from "../i18n";
import type { TranslationKey } from "../i18n";

const { Text } = Typography;

type KindFilter = "all" | "alert" | "report";

const KIND_LABEL_KEY: Record<string, { labelKey: TranslationKey; color: string }> = {
  alert: { labelKey: "templates.kindAlert", color: "blue" },
  report: { labelKey: "templates.kindReport", color: "green" },
};

function KindTag({ kind }: { kind: string }) {
  const { t } = useI18n();
  const meta = KIND_LABEL_KEY[kind];
  return <Tag color={meta?.color ?? "default"}>{meta ? t(meta.labelKey) : kind}</Tag>;
}

export default function Templates() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { message } = App.useApp();
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const { currentTeam } = useTeam();
  const [kindFilter, setKindFilter] = useState<KindFilter>("all");

  const isOwner = useMemo(() => {
    if (!user || !currentTeam) return false;
    if (user.is_admin) return true;
    const membership = user.teams.find((t) => t.id === currentTeam.id);
    return membership?.role === "owner";
  }, [user, currentTeam]);

  const teamId = currentTeam?.id;

  const templatesQuery = useQuery({
    queryKey: ["templates", teamId],
    queryFn: () => listTemplates(teamId!),
    enabled: !!teamId,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => deleteTemplate(id),
    onSuccess: (result) => {
      message.success(result.detail);
      queryClient.invalidateQueries({ queryKey: ["templates", teamId] });
      // A deleted template's channels/routes revert to their next-priority
      // default server-side -- their cached template_id would otherwise
      // keep pointing at a now-gone row until something else refetches them.
      queryClient.invalidateQueries({ queryKey: ["channels", teamId] });
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
        <h2>{t("templates.title")}</h2>
        <Alert
          type="info"
          showIcon
          message={t("common.noTeamAssigned")}
          description={t("common.requestTeamAssignment")}
        />
      </div>
    );
  }

  const allTemplates = templatesQuery.data ?? [];
  const templates =
    kindFilter === "all" ? allTemplates : allTemplates.filter((t) => t.kind === kindFilter);

  const columns = [
    { title: t("common.name"), dataIndex: "name", key: "name" },
    {
      title: t("templates.kindColumn"),
      dataIndex: "kind",
      key: "kind",
      render: (kind: string) => <KindTag kind={kind} />,
    },
    {
      title: t("common.description"),
      dataIndex: "description",
      key: "description",
      render: (description: string | null) => description ?? <Text type="secondary">-</Text>,
    },
    {
      title: t("templates.usageColumn"),
      key: "usage",
      render: (_: unknown, template: MessageTemplate) => (
        <Space size={4}>
          {template.channel_count > 0 && (
            <Tag color="blue">{t("templates.channelUsage", { count: template.channel_count })}</Tag>
          )}
          {template.route_count > 0 && (
            <Tag color="purple">{t("templates.routeUsage", { count: template.route_count })}</Tag>
          )}
          {template.channel_count === 0 && template.route_count === 0 && (
            <Text type="secondary">{t("templates.notUsed")}</Text>
          )}
        </Space>
      ),
    },
    {
      title: "",
      key: "actions",
      render: (_: unknown, template: MessageTemplate) =>
        isOwner ? (
          <div style={{ display: "flex", gap: 8 }}>
            <Button size="small" onClick={() => navigate(`/templates/${template.id}/edit`)}>
              {t("common.edit")}
            </Button>
            <Popconfirm
              title={t("templates.deleteConfirm")}
              description={
                template.channel_count + template.route_count > 0
                  ? t("templates.deleteConfirmDetail", {
                      channelCount: template.channel_count,
                      routeCount: template.route_count,
                    })
                  : undefined
              }
              onConfirm={() => deleteMutation.mutate(template.id)}
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
        <h2 style={{ margin: 0 }}>{t("templates.titleWithTeam", { team: currentTeam.name })}</h2>
        {isOwner && (
          <Button type="primary" onClick={() => navigate("/templates/new")}>
            {t("templates.createButton")}
          </Button>
        )}
      </div>

      <Segmented
        style={{ marginBottom: 16 }}
        value={kindFilter}
        onChange={(value) => setKindFilter(value as KindFilter)}
        options={[
          { label: t("common.all"), value: "all" },
          { label: t("templates.kindAlert"), value: "alert" },
          { label: t("templates.kindReport"), value: "report" },
        ]}
      />

      <Table<MessageTemplate>
        rowKey="id"
        loading={templatesQuery.isLoading}
        dataSource={templates}
        columns={columns}
        pagination={false}
        locale={{ emptyText: <Empty description={t("templates.empty")} /> }}
      />
    </div>
  );
}
