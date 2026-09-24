import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App, Button, Empty, Popconfirm, Segmented, Space, Table, Tag, Typography } from "antd";
import { useNavigate } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { ApiError } from "../api/client";
import { deleteTemplate, listTemplates } from "../api/templates";
import type { MessageTemplate } from "../api/templates";

const { Text } = Typography;

type KindFilter = "all" | "alert" | "report";

const KIND_LABELS: Record<string, { label: string; color: string }> = {
  alert: { label: "알럿", color: "blue" },
  report: { label: "리포트", color: "green" },
};

function KindTag({ kind }: { kind: string }) {
  const meta = KIND_LABELS[kind] ?? { label: kind, color: "default" };
  return <Tag color={meta.color}>{meta.label}</Tag>;
}

export default function Templates() {
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
        err instanceof ApiError ? `삭제에 실패했습니다: ${err.detail}` : "삭제에 실패했습니다",
      );
    },
  });

  if (!currentTeam) {
    return (
      <div>
        <h2>템플릿</h2>
        <Alert
          type="info"
          showIcon
          message="소속된 팀이 없습니다"
          description="관리자에게 팀 추가를 요청하세요."
        />
      </div>
    );
  }

  const allTemplates = templatesQuery.data ?? [];
  const templates =
    kindFilter === "all" ? allTemplates : allTemplates.filter((t) => t.kind === kindFilter);

  const columns = [
    { title: "이름", dataIndex: "name", key: "name" },
    {
      title: "종류",
      dataIndex: "kind",
      key: "kind",
      render: (kind: string) => <KindTag kind={kind} />,
    },
    {
      title: "설명",
      dataIndex: "description",
      key: "description",
      render: (description: string | null) => description ?? <Text type="secondary">-</Text>,
    },
    {
      title: "사용처",
      key: "usage",
      render: (_: unknown, template: MessageTemplate) => (
        <Space size={4}>
          {template.channel_count > 0 && <Tag color="blue">채널 {template.channel_count}</Tag>}
          {template.route_count > 0 && <Tag color="purple">규칙 {template.route_count}</Tag>}
          {template.channel_count === 0 && template.route_count === 0 && (
            <Text type="secondary">사용 안 함</Text>
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
              수정
            </Button>
            <Popconfirm
              title="이 템플릿을 삭제하시겠습니까?"
              description={
                template.channel_count + template.route_count > 0
                  ? `사용 중인 채널 ${template.channel_count}개, 규칙 ${template.route_count}개는 기본 템플릿으로 되돌아갑니다.`
                  : undefined
              }
              onConfirm={() => deleteMutation.mutate(template.id)}
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
        <h2 style={{ margin: 0 }}>템플릿 — {currentTeam.name}</h2>
        {isOwner && (
          <Button type="primary" onClick={() => navigate("/templates/new")}>
            템플릿 생성
          </Button>
        )}
      </div>

      <Segmented
        style={{ marginBottom: 16 }}
        value={kindFilter}
        onChange={(value) => setKindFilter(value as KindFilter)}
        options={[
          { label: "전체", value: "all" },
          { label: "알럿", value: "alert" },
          { label: "리포트", value: "report" },
        ]}
      />

      <Table<MessageTemplate>
        rowKey="id"
        loading={templatesQuery.isLoading}
        dataSource={templates}
        columns={columns}
        pagination={false}
        locale={{ emptyText: <Empty description="템플릿이 없습니다" /> }}
      />
    </div>
  );
}
