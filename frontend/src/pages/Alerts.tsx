import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import {
  Alert,
  Badge,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Segmented,
  Select,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
} from "antd";
import { Link } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { getAckStatus, getLiveAlerts } from "../api/alerts";
import type { AckStatusMatch, LiveAlert } from "../api/alerts";
import { listClusters } from "../api/admin";
import type { MatcherInput } from "../api/silences";
import SilenceModal from "../components/SilenceModal";
import { severityColor, severityTagStyle } from "../theme";
import { useI18n } from "../i18n";

dayjs.extend(relativeTime);

const { Text, Title } = Typography;

type StateFilter = "all" | "active" | "suppressed";

export default function Alerts() {
  const { t } = useI18n();
  const { user } = useAuth();
  const { currentTeam, teams } = useTeam();
  const { clusters, selectedIds: clusterIds } = useClusterFilter();
  const isAdmin = !!user?.is_admin;

  const SEVERITY_OPTIONS = [
    { value: "critical", label: "critical" },
    { value: "warning", label: "warning" },
    { value: "info", label: "info" },
    { value: "none", label: t("common.none") },
  ];

  const [severity, setSeverity] = useState<string[]>([]);
  const [namespace, setNamespace] = useState<string | undefined>(undefined);
  const [stateFilter, setStateFilter] = useState<StateFilter>("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<LiveAlert | null>(null);
  const [silenceModalOpen, setSilenceModalOpen] = useState(false);

  const teamId = currentTeam?.id;
  const noTeamSelected = !isAdmin && teams.length === 0;

  // Phase 17: a dedicated, 30s-polled read of GET /clusters for the missing-
  // heartbeat banner below -- shares its ["clusters"] cache with
  // useClusterFilter's own query (same key, same fetcher), but adds its own
  // independent refetch cadence on top so the banner notices a cluster
  // going missing (or recovering) without the user having to reload.
  const heartbeatQuery = useQuery({
    queryKey: ["clusters"],
    queryFn: listClusters,
    refetchInterval: 30_000,
  });
  const missingClusters = useMemo(
    () =>
      (heartbeatQuery.data ?? []).filter((c) => {
        if (c.heartbeat_state !== "missing") return false;
        if (!c.enabled) return false;
        // `heartbeat_enabled` is admin-only in GET /clusters (see
        // app/api/clusters.py's _serialize_cluster) -- undefined for a
        // non-admin viewer, so this only excludes when it's known to be
        // explicitly false, never when it's simply not visible to this
        // user's role. In steady state a disabled/heartbeat-disabled
        // cluster's state is reset away from 'missing' the moment it's
        // toggled (app/api/clusters.py's update_cluster), so this is
        // belt-and-suspenders against a row that predates that reset.
        if (c.heartbeat_enabled === false) return false;
        return true;
      }),
    [heartbeatQuery.data],
  );

  // Silence creation needs a numeric cluster_id, but the live-alerts
  // fan-out only carries the cluster's name (it spans every enabled
  // cluster) -- so the selected alert's cluster name is cross-referenced
  // against the full cluster list here. Shares the ["clusters"] query key
  // with ClusterFilterContext, so this doesn't add an extra round trip
  // beyond what the app already fetches elsewhere.
  const selectedClusterId = useMemo(
    () => clusters.find((c) => c.name === selected?.cluster)?.id,
    [clusters, selected],
  );
  const silenceMatchers: MatcherInput[] = useMemo(
    () =>
      selected
        ? Object.entries(selected.labels).map(([name, value]) => ({
            name,
            value,
            is_regex: false,
          }))
        : [],
    [selected],
  );

  const query = useQuery({
    queryKey: ["alerts-live", teamId, clusterIds, severity, namespace, stateFilter, search],
    queryFn: () =>
      getLiveAlerts({
        teamId,
        clusterIds: clusterIds.length > 0 ? clusterIds : undefined,
        severity: severity.length > 0 ? severity : undefined,
        namespace,
        state: stateFilter === "all" ? undefined : stateFilter,
        search: search || undefined,
      }),
    enabled: isAdmin || !!teamId,
    refetchInterval: 30_000,
  });

  const alerts = useMemo(() => query.data?.alerts ?? [], [query.data]);
  const errors = query.data?.errors ?? [];

  const ackStatusQuery = useQuery({
    queryKey: ["ack-status", teamId, alerts.map((a) => `${a.cluster}|${a.fingerprint}`)],
    queryFn: () =>
      getAckStatus(
        teamId,
        alerts.map((a) => ({ cluster: a.cluster, fingerprint: a.fingerprint })),
      ),
    enabled: (isAdmin || !!teamId) && alerts.length > 0,
  });

  const ackByKey = useMemo(() => {
    const map = new Map<string, AckStatusMatch>();
    for (const match of ackStatusQuery.data?.matched ?? []) {
      map.set(`${match.cluster}|${match.fingerprint}`, match);
    }
    return map;
  }, [ackStatusQuery.data]);

  const selectedAck = selected ? ackByKey.get(`${selected.cluster}|${selected.fingerprint}`) : undefined;

  const namespaceOptions = useMemo(() => {
    const seen = new Set<string>();
    for (const alert of alerts) {
      if (alert.namespace) seen.add(alert.namespace);
    }
    return Array.from(seen)
      .sort()
      .map((ns) => ({ value: ns, label: ns }));
  }, [alerts]);

  if (noTeamSelected) {
    return (
      <div>
        <h2>{t("alerts.title")}</h2>
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
      dataIndex: "state",
      key: "state",
      width: 110,
      render: (state: string) =>
        state === "active" ? (
          <Badge status="processing" color={severityColor("critical")} text="active" />
        ) : (
          <Badge status="default" text={state || "-"} />
        ),
    },
    {
      title: t("alerts.alertName"),
      dataIndex: "alertname",
      key: "alertname",
      render: (value: string, record: LiveAlert) => (
        <Space size={4}>
          {value}
          {record.shared_from && (
            <Tag color="blue">{t("alerts.sharedFrom", { source: record.shared_from })}</Tag>
          )}
        </Space>
      ),
    },
    {
      title: t("common.severity"),
      dataIndex: "severity",
      key: "severity",
      render: (value: string) => <Tag style={severityTagStyle(value)}>{value || "none"}</Tag>,
    },
    { title: t("common.namespace"), dataIndex: "namespace", key: "namespace" },
    { title: t("common.cluster"), dataIndex: "cluster", key: "cluster" },
    {
      title: t("common.acknowledged"),
      key: "acknowledged",
      width: 70,
      align: "center" as const,
      render: (_: unknown, record: LiveAlert) => {
        const match = ackByKey.get(`${record.cluster}|${record.fingerprint}`);
        if (!match?.acknowledged) return <Text type="secondary">-</Text>;
        return (
          <Tooltip
            title={
              match.assignee_username
                ? t("common.assigneeWithName", { name: match.assignee_username })
                : t("common.acknowledgedState")
            }
          >
            <Tag color="success">✓</Tag>
          </Tooltip>
        );
      },
    },
    {
      title: t("common.startedAt"),
      dataIndex: "starts_at",
      key: "starts_at",
      render: (startsAt: string) => (
        <Tooltip title={dayjs(startsAt).format("YYYY-MM-DD HH:mm:ss")}>
          {dayjs(startsAt).fromNow()}
        </Tooltip>
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
          {t("alerts.title")}{" "}
          <Text type="secondary" style={{ fontSize: 14, fontWeight: "normal" }}>
            ({t("alerts.count", { count: alerts.length })})
          </Text>
        </h2>
        <div style={{ display: "flex", gap: 8 }}>
          <Link to="/alerts/history">
            <Button>{t("alerts.viewHistory")}</Button>
          </Link>
          <Button onClick={() => query.refetch()} loading={query.isFetching}>
            {t("common.refresh")}
          </Button>
        </div>
      </div>

      {missingClusters.length > 0 && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("alerts.missingHeartbeat", {
            clusters: missingClusters.map((c) => c.display_name).join(", "),
          })}
        />
      )}

      {errors.map((err) => (
        <Alert
          key={err.cluster}
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message={t("alerts.clusterFetchError", { cluster: err.cluster, message: err.message })}
        />
      ))}

      <div style={{ display: "flex", gap: 12, marginBottom: 16, flexWrap: "wrap" }}>
        <Select
          mode="multiple"
          allowClear
          placeholder={t("common.severity")}
          style={{ minWidth: 220 }}
          options={SEVERITY_OPTIONS}
          value={severity}
          onChange={setSeverity}
        />
        <Select
          allowClear
          placeholder={t("common.namespace")}
          style={{ minWidth: 200 }}
          options={namespaceOptions}
          value={namespace}
          onChange={setNamespace}
        />
        <Segmented<StateFilter>
          value={stateFilter}
          onChange={setStateFilter}
          options={[
            { label: t("common.all"), value: "all" },
            { label: "active", value: "active" },
            { label: "suppressed", value: "suppressed" },
          ]}
        />
        <Input.Search
          placeholder={t("alerts.searchPlaceholder")}
          allowClear
          style={{ minWidth: 240 }}
          onSearch={setSearch}
        />
      </div>

      <Table<LiveAlert>
        rowKey="fingerprint"
        loading={query.isLoading}
        dataSource={alerts}
        columns={columns}
        pagination={{ pageSize: 20 }}
        onRow={(record) => ({
          onClick: () => setSelected(record),
          style: { cursor: "pointer" },
        })}
        locale={{ emptyText: <Empty description={t("alerts.empty")} /> }}
      />

      <Drawer
        title={selected?.alertname}
        open={!!selected}
        onClose={() => setSelected(null)}
        width={480}
        extra={
          <Space>
            {selectedAck?.event_id && (
              <Link to={`/alerts/history?highlight=${selectedAck.event_id}`}>
                {t("alerts.viewInHistory")}
              </Link>
            )}
            <Tooltip title={currentTeam ? undefined : t("common.noTeamAssigned")}>
              <Button disabled={!currentTeam} onClick={() => setSilenceModalOpen(true)}>
                {t("alerts.silenceThisAlert")}
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {selected && (
          <>
            <Descriptions column={1} bordered size="small" style={{ marginBottom: 24 }}>
              <Descriptions.Item label={t("common.status")}>{selected.state}</Descriptions.Item>
              <Descriptions.Item label={t("common.severity")}>
                <Tag style={severityTagStyle(selected.severity)}>{selected.severity || "none"}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label={t("common.namespace")}>{selected.namespace}</Descriptions.Item>
              {selectedAck?.acknowledged && (
                <Descriptions.Item label={t("common.acknowledged")}>
                  <Tag color="success">
                    {t("common.acknowledgedState")}
                    {selectedAck.assignee_username
                      ? ` · ${t("common.assigneeWithName", { name: selectedAck.assignee_username })}`
                      : ""}
                  </Tag>
                </Descriptions.Item>
              )}
              <Descriptions.Item label={t("common.cluster")}>{selected.cluster}</Descriptions.Item>
              <Descriptions.Item label={t("common.startedAt")}>
                {dayjs(selected.starts_at).format("YYYY-MM-DD HH:mm:ss")}
              </Descriptions.Item>
              <Descriptions.Item label={t("alerts.silencedBy")}>
                {selected.silenced_by.length > 0 ? (
                  selected.silenced_by.map((id) => (
                    <Tag key={id} style={{ marginBottom: 4 }}>
                      {id}
                    </Tag>
                  ))
                ) : (
                  <Text type="secondary">-</Text>
                )}
              </Descriptions.Item>
            </Descriptions>

            <Title level={5}>{t("alerts.labelsTitle")}</Title>
            <div style={{ marginBottom: 24 }}>
              {Object.entries(selected.labels).map(([key, value]) => (
                <Tag key={key} style={{ marginBottom: 4 }}>
                  {key}={value}
                </Tag>
              ))}
            </div>

            <Title level={5}>{t("alerts.annotationsTitle")}</Title>
            <div style={{ whiteSpace: "pre-wrap", marginBottom: 24 }}>
              {Object.entries(selected.annotations).length > 0 ? (
                Object.entries(selected.annotations).map(([key, value]) => (
                  <div key={key} style={{ marginBottom: 8 }}>
                    <Text strong>{key}: </Text>
                    {value}
                  </div>
                ))
              ) : (
                <Text type="secondary">-</Text>
              )}
            </div>

            <Space size="middle">
              {selected.generator_url && (
                <a href={selected.generator_url} target="_blank" rel="noreferrer">
                  {t("alerts.viewInPrometheus")}
                </a>
              )}
              {selected.grafana_url && (
                <a href={selected.grafana_url} target="_blank" rel="noreferrer">
                  {t("alerts.viewInGrafana")}
                </a>
              )}
            </Space>
          </>
        )}
      </Drawer>

      {selected && currentTeam && (
        <SilenceModal
          open={silenceModalOpen}
          onClose={() => setSilenceModalOpen(false)}
          clusters={clusters}
          initialClusterId={selectedClusterId}
          team={currentTeam}
          initialMatchers={silenceMatchers}
        />
      )}
    </div>
  );
}
