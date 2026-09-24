import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import dayjs, { type Dayjs } from "dayjs";
import ReactECharts from "echarts-for-react";
import { Alert, Card, Col, DatePicker, Empty, Row, Select, Statistic, Typography } from "antd";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { ApiError } from "../api/client";
import { listTeams } from "../api/teams";
import {
  getBreakdown,
  getResponseTimes,
  getStatsSummary,
  getTopAlerts,
  getVolume,
  type StatsFilters,
  type VolumeBucket,
} from "../api/stats";
import { chartCategorical, palette, severity, severityColor } from "../theme";
import { useI18n } from "../i18n";
type TFn = ReturnType<typeof useI18n>["t"];

const { RangePicker } = DatePicker;
const { Text } = Typography;

// Severity slices use the app-wide semantic severity tokens (same hues as
// the Tag columns on Alerts/History); single-series ranking bars and the
// volume area use the first categorical hue -- ranking compares magnitude,
// not identity, so one hue is correct.
const CHART_COLOR = chartCategorical[0];

// Recessive chart chrome shared by every cartesian chart on this page: the
// data is the only assertive layer, grid/axes stay hairline + muted.
const AXIS_LABEL = { color: palette.inkMuted, fontSize: 11 };
const AXIS_LINE = { lineStyle: { color: palette.hairline } };
const SPLIT_LINE = { lineStyle: { color: palette.hairline } };
const CHART_ANIMATION = { animationDuration: 200, animationDurationUpdate: 200 };

function apiErrorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

/** Seconds -> a human-scaled duration: seconds under a minute, "Nm Ms" under
 * an hour, "Nh Mm" beyond that -- an MTTR can genuinely span days, and a
 * flat "N min M sec" there would read as an implausible four-digit minute
 * count. */
function formatDuration(seconds: number | null, t: TFn): string {
  if (seconds === null) return "-";
  const total = Math.round(seconds);
  if (total < 60) return t("stats.durationSeconds", { value: total });
  if (total < 3600) {
    const m = Math.floor(total / 60);
    const s = total % 60;
    return t("stats.durationMinSec", { m, s });
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  return t("stats.durationHourMin", { h, m });
}

export default function Stats() {
  const { t } = useI18n();
  const RANGE_PRESETS: { label: string; value: [Dayjs, Dayjs] }[] = [
    { label: t("stats.presetToday"), value: [dayjs().startOf("day"), dayjs()] },
    { label: t("stats.presetLast7Days"), value: [dayjs().subtract(7, "day"), dayjs()] },
    { label: t("stats.presetLast30Days"), value: [dayjs().subtract(30, "day"), dayjs()] },
    { label: t("stats.presetLast90Days"), value: [dayjs().subtract(90, "day"), dayjs()] },
  ];
  const { user } = useAuth();
  const { currentTeam, teams } = useTeam();
  const { selectedIds: clusterIds } = useClusterFilter();
  const isAdmin = !!user?.is_admin;

  const [range, setRange] = useState<[Dayjs, Dayjs]>([dayjs().subtract(7, "day"), dayjs()]);
  // Admin only -- undefined means "every team". Non-admins are always
  // pinned to their current team, same as AlertHistory/Alerts.
  const [adminTeamId, setAdminTeamId] = useState<number | undefined>(undefined);

  const teamsQuery = useQuery({ queryKey: ["teams"], queryFn: listTeams, enabled: isAdmin });

  const teamId = isAdmin ? adminTeamId : currentTeam?.id;
  const noTeamSelected = !isAdmin && teams.length === 0;

  const [fromTs, toTs] = range;

  const filters: StatsFilters = useMemo(
    () => ({
      teamId,
      clusterIds: clusterIds.length > 0 ? clusterIds : undefined,
      fromTs: fromTs.toISOString(),
      toTs: toTs.toISOString(),
    }),
    [teamId, clusterIds, fromTs, toTs],
  );

  // Fine enough resolution to see intraday spikes on a short window, without
  // rendering an unreadable forest of hourly bars over a longer one.
  const bucket: VolumeBucket = toTs.diff(fromTs, "day", true) <= 2 ? "hour" : "day";

  const enabled = isAdmin || !!teamId;

  const summaryQuery = useQuery({
    queryKey: ["stats-summary", filters],
    queryFn: () => getStatsSummary(filters),
    enabled,
  });
  const responseTimesQuery = useQuery({
    queryKey: ["stats-response-times", filters],
    queryFn: () => getResponseTimes(filters),
    enabled,
  });
  const topAlertsQuery = useQuery({
    queryKey: ["stats-top-alerts", filters],
    queryFn: () => getTopAlerts(filters, 10),
    enabled,
  });
  const volumeQuery = useQuery({
    queryKey: ["stats-volume", filters, bucket],
    queryFn: () => getVolume(filters, bucket),
    enabled,
  });
  const severityQuery = useQuery({
    queryKey: ["stats-breakdown-severity", filters],
    queryFn: () => getBreakdown(filters, "severity"),
    enabled,
  });
  const namespaceQuery = useQuery({
    queryKey: ["stats-breakdown-namespace", filters],
    queryFn: () => getBreakdown(filters, "namespace"),
    enabled,
  });

  // All six queries share the exact same `filters` (team/cluster/range), so
  // a range-validation error (e.g. the 90-day cap) hits every one of them
  // identically -- watching all six here means the breakdown cards' own
  // failures surface in this one banner too, instead of silently rendering
  // as an empty "데이터가 없습니다" state below (which looks like "no data
  // in this range" rather than "the request itself failed").
  const rangeError = [
    summaryQuery,
    responseTimesQuery,
    topAlertsQuery,
    volumeQuery,
    severityQuery,
    namespaceQuery,
  ].find((q) => q.isError)?.error;

  if (noTeamSelected) {
    return (
      <div>
        <h2>{t("nav.stats")}</h2>
        <Alert
          type="info"
          showIcon
          message={t("common.noTeamAssigned")}
          description={t("common.requestTeamAssignment")}
        />
      </div>
    );
  }

  const topAlerts = topAlertsQuery.data ?? [];
  const volume = volumeQuery.data ?? [];
  const severityRows = severityQuery.data ?? [];
  const namespace = (namespaceQuery.data ?? []).slice(0, 10);

  return (
    <div>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
          flexWrap: "wrap",
          gap: 12,
        }}
      >
        <h2 style={{ margin: 0 }}>{t("nav.stats")}</h2>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          {isAdmin && (
            <Select
              allowClear
              placeholder={t("stats.allTeamsPlaceholder")}
              style={{ minWidth: 180 }}
              loading={teamsQuery.isLoading}
              value={adminTeamId}
              onChange={setAdminTeamId}
              options={(teamsQuery.data ?? []).map((t) => ({ value: t.id, label: t.name }))}
            />
          )}
          <RangePicker
            value={range}
            allowClear={false}
            presets={RANGE_PRESETS}
            onChange={(value) => {
              if (value && value[0] && value[1]) setRange([value[0], value[1]]);
            }}
          />
        </div>
      </div>

      {rangeError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={apiErrorMessage(rangeError, t("stats.loadError"))}
        />
      )}

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title={t("stats.firingNowLabel")}
              value={summaryQuery.data?.firing_now ?? 0}
              loading={summaryQuery.isLoading}
              valueStyle={{ color: severity.warning, fontWeight: 600 }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title={t("stats.eventsInRangeLabel")}
              value={summaryQuery.data?.events_in_range ?? 0}
              loading={summaryQuery.isLoading}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title={t("stats.deliveredLabel")}
              value={summaryQuery.data?.delivered_in_range ?? 0}
              loading={summaryQuery.isLoading}
              valueStyle={{ color: severity.ok, fontWeight: 600 }}
            />
          </Card>
        </Col>
        <Col xs={12} md={6}>
          <Card size="small">
            <Statistic
              title={t("stats.failedOrDeadLabel")}
              value={summaryQuery.data?.failed_or_dead_in_range ?? 0}
              loading={summaryQuery.isLoading}
              valueStyle={{ color: severity.critical, fontWeight: 600 }}
            />
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} md={12}>
          <Card size="small" title={t("stats.mttaTitle")} loading={responseTimesQuery.isLoading}>
            <Statistic value={formatDuration(responseTimesQuery.data?.mtta_seconds ?? null, t)} />
            <Text type="secondary">
              {t("stats.ackedBasis", { count: responseTimesQuery.data?.acked_count ?? 0 })}
            </Text>
          </Card>
        </Col>
        <Col xs={24} md={12}>
          <Card size="small" title={t("stats.mttrTitle")} loading={responseTimesQuery.isLoading}>
            <Statistic value={formatDuration(responseTimesQuery.data?.mttr_seconds ?? null, t)} />
            <Text type="secondary">
              {t("stats.resolvedBasis", { count: responseTimesQuery.data?.resolved_count ?? 0 })}
            </Text>
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]} style={{ marginBottom: 16 }}>
        <Col xs={24} lg={12}>
          <Card size="small" title={t("stats.topAlertsTitle")} loading={topAlertsQuery.isLoading}>
            {topAlerts.length === 0 ? (
              <Empty description={t("stats.noData")} />
            ) : (
              <ReactECharts
                style={{ height: Math.max(240, topAlerts.length * 32) }}
                notMerge
                option={{
                  tooltip: {
                    trigger: "item",
                    formatter: (p: { name: string; value: number; dataIndex: number }) => {
                      const row = topAlerts[p.dataIndex];
                      return t("stats.topAlertsTooltip", {
                        name: p.name,
                        count: p.value,
                        receiveCount: row?.receive_total ?? 0,
                      });
                    },
                  },
                  ...CHART_ANIMATION,
                  grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
                  xAxis: {
                    type: "value",
                    name: t("stats.countAxisLabel"),
                    axisLabel: AXIS_LABEL,
                    nameTextStyle: AXIS_LABEL,
                    splitLine: SPLIT_LINE,
                  },
                  yAxis: {
                    type: "category",
                    inverse: true,
                    data: topAlerts.map((r) => r.alertname),
                    axisLabel: { ...AXIS_LABEL, fontSize: 12 },
                    axisLine: AXIS_LINE,
                    axisTick: { show: false },
                  },
                  series: [
                    {
                      type: "bar",
                      data: topAlerts.map((r) => r.count),
                      itemStyle: { color: CHART_COLOR, borderRadius: [0, 4, 4, 0] },
                      barMaxWidth: 24,
                    },
                  ],
                }}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card
            size="small"
            title={t("stats.volumeTrendTitle", {
              granularity: bucket === "hour" ? t("stats.granularityHourly") : t("stats.granularityDaily"),
            })}
            loading={volumeQuery.isLoading}
          >
            {volume.length === 0 ? (
              <Empty description={t("stats.noData")} />
            ) : (
              <ReactECharts
                style={{ height: 280 }}
                notMerge
                option={{
                  tooltip: {
                    trigger: "axis",
                    valueFormatter: (v: number) => t("alerts.count", { count: v }),
                  },
                  ...CHART_ANIMATION,
                  grid: { left: 48, right: 24, top: 24, bottom: 32 },
                  xAxis: { type: "time", axisLabel: AXIS_LABEL, axisLine: AXIS_LINE },
                  yAxis: {
                    type: "value",
                    name: t("stats.countAxisLabel"),
                    minInterval: 1,
                    axisLabel: AXIS_LABEL,
                    nameTextStyle: AXIS_LABEL,
                    splitLine: SPLIT_LINE,
                  },
                  series: [
                    {
                      type: "line",
                      showSymbol: false,
                      lineStyle: { width: 2, color: CHART_COLOR },
                      areaStyle: { color: CHART_COLOR, opacity: 0.12 },
                      data: volume.map((v) => [dayjs(v.bucket_start).valueOf(), v.firing_count]),
                    },
                  ],
                }}
              />
            )}
          </Card>
        </Col>
      </Row>

      <Row gutter={[16, 16]}>
        <Col xs={24} lg={12}>
          <Card size="small" title={t("stats.severityDistributionTitle")} loading={severityQuery.isLoading}>
            {severityRows.length === 0 ? (
              <Empty description={t("stats.noData")} />
            ) : (
              <ReactECharts
                style={{ height: 280 }}
                notMerge
                option={{
                  ...CHART_ANIMATION,
                  tooltip: { trigger: "item", formatter: t("stats.severityPieTooltip") },
                  legend: { bottom: 0, textStyle: { color: palette.inkMuted } },
                  series: [
                    {
                      type: "pie",
                      radius: ["45%", "70%"],
                      avoidLabelOverlap: true,
                      label: { formatter: "{b}\n{d}%", color: palette.ink },
                      // Status colors, not the categorical palette -- a
                      // severity slice must match the severity Tag next to it.
                      data: severityRows.map((row) => ({
                        name: row.key,
                        value: row.count,
                        itemStyle: {
                          color: severityColor(row.key),
                          borderColor: palette.surface,
                          borderWidth: 2,
                        },
                      })),
                    },
                  ],
                }}
              />
            )}
          </Card>
        </Col>
        <Col xs={24} lg={12}>
          <Card size="small" title={t("stats.namespaceTopTitle")} loading={namespaceQuery.isLoading}>
            {namespace.length === 0 ? (
              <Empty description={t("stats.noData")} />
            ) : (
              <ReactECharts
                style={{ height: Math.max(240, namespace.length * 32) }}
                notMerge
                option={{
                  ...CHART_ANIMATION,
                  tooltip: { trigger: "item", valueFormatter: (v: number) => t("alerts.count", { count: v }) },
                  grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
                  xAxis: {
                    type: "value",
                    name: t("stats.countAxisLabel"),
                    axisLabel: AXIS_LABEL,
                    nameTextStyle: AXIS_LABEL,
                    splitLine: SPLIT_LINE,
                  },
                  yAxis: {
                    type: "category",
                    inverse: true,
                    data: namespace.map((r) => r.key),
                    axisLabel: { ...AXIS_LABEL, fontSize: 12 },
                    axisLine: AXIS_LINE,
                    axisTick: { show: false },
                  },
                  series: [
                    {
                      type: "bar",
                      data: namespace.map((r) => r.count),
                      itemStyle: { color: CHART_COLOR, borderRadius: [0, 4, 4, 0] },
                      barMaxWidth: 24,
                    },
                  ],
                }}
              />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  );
}
