import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { Alert, Segmented, Space, Tag, Typography } from "antd";
import { ApiError } from "../../api/client";
import { runInstantQuery, runQueryRange } from "../../api/metrics";
import type { RangeSeries } from "../../api/metrics";
import { useI18n } from "../../i18n";
import type { TranslationKey } from "../../i18n";

const { Text } = Typography;

type RangeKey = "1h" | "6h" | "24h";

const RANGE_OPTION_KEYS: { labelKey: TranslationKey; value: RangeKey; seconds: number }[] = [
  { labelKey: "preview.range1h", value: "1h", seconds: 3600 },
  { labelKey: "preview.range6h", value: "6h", seconds: 6 * 3600 },
  { labelKey: "preview.range24h", value: "24h", seconds: 24 * 3600 },
];

interface PreviewChartProps {
  clusterId: number | undefined;
  /** Queried by query_range to draw the chart -- the metric{labels}
   * selector alone in builder mode (so the line traces the metric's actual
   * values, not a 0/1 boolean), or the raw expression in PromQL mode. */
  chartExpr: string;
  /** Queried by the instant "지금 발생?" check -- the full alerting
   * expression, comparison included. */
  fullExpr: string;
  /** Drawn as a horizontal markLine; only known (and only meaningful) in
   * builder mode. */
  threshold?: number;
}

function compactLabelName(labels: Record<string, string>): string {
  const entries = Object.entries(labels);
  if (entries.length === 0) return "value";
  return entries.map(([k, v]) => `${k}=${v}`).join(", ");
}

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail : fallback;
}

export default function PreviewChart({ clusterId, chartExpr, fullExpr, threshold }: PreviewChartProps) {
  const { t } = useI18n();
  const RANGE_OPTIONS = RANGE_OPTION_KEYS.map((r) => ({ ...r, label: t(r.labelKey) }));
  const [range, setRange] = useState<RangeKey>("1h");
  const rangeSeconds = RANGE_OPTIONS.find((r) => r.value === range)?.seconds ?? 3600;

  const trimmedChartExpr = chartExpr.trim();
  const trimmedFullExpr = fullExpr.trim();

  const rangeQuery = useQuery({
    queryKey: ["metrics-preview-range", clusterId, trimmedChartExpr, range],
    queryFn: () => {
      const end = Math.floor(Date.now() / 1000);
      const start = end - rangeSeconds;
      return runQueryRange(clusterId!, trimmedChartExpr, start, end);
    },
    enabled: !!clusterId && !!trimmedChartExpr,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const instantQuery = useQuery({
    queryKey: ["metrics-preview-instant", clusterId, trimmedFullExpr],
    queryFn: () => runInstantQuery(clusterId!, trimmedFullExpr),
    enabled: !!clusterId && !!trimmedFullExpr,
    refetchOnWindowFocus: false,
    retry: false,
  });

  const series: RangeSeries[] = rangeQuery.data?.series ?? [];

  const option = useMemo(() => {
    return {
      tooltip: { trigger: "axis" },
      grid: { left: 56, right: 24, top: 24, bottom: series.length > 1 ? 64 : 32 },
      legend: series.length > 1 ? { type: "scroll", bottom: 0 } : undefined,
      dataZoom: [{ type: "inside" }, { type: "slider", height: 16, bottom: series.length > 1 ? 28 : 0 }],
      xAxis: { type: "time" },
      yAxis: { type: "value", scale: true },
      series: series.map((s, index) => ({
        name: compactLabelName(s.labels),
        type: "line",
        showSymbol: false,
        data: s.points.map(([ts, v]) => [ts * 1000, v]),
        ...(index === 0 && threshold !== undefined
          ? {
              markLine: {
                symbol: "none",
                silent: true,
                lineStyle: { color: "#f5222d", type: "dashed" },
                data: [
                  { yAxis: threshold, label: { formatter: t("preview.thresholdMarkLabel", { value: threshold }) } },
                ],
              },
            }
          : {}),
      })),
    };
  }, [series, threshold, t]);

  const omittedCount = (rangeQuery.data?.total_series ?? 0) - series.length;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <Segmented<RangeKey>
          value={range}
          onChange={setRange}
          options={RANGE_OPTIONS.map((r) => ({ label: r.label, value: r.value }))}
        />
        <NowFiringPill
          isLoading={instantQuery.isLoading}
          isError={instantQuery.isError}
          error={instantQuery.error}
          seriesCount={instantQuery.data?.series_count}
          enabled={!!trimmedFullExpr}
        />
      </div>

      {rangeQuery.isError && (
        <Alert
          type="error"
          showIcon
          message={errorMessage(rangeQuery.error, t("preview.loadError"))}
          style={{ marginBottom: 8 }}
        />
      )}
      {rangeQuery.data?.truncated && omittedCount > 0 && (
        <Alert
          type="warning"
          showIcon
          message={t("preview.seriesOmitted", { count: omittedCount })}
          style={{ marginBottom: 8 }}
        />
      )}

      {trimmedChartExpr ? (
        <ReactECharts
          option={option}
          style={{ height: 320 }}
          notMerge
          showLoading={rangeQuery.isFetching}
        />
      ) : (
        <div style={{ height: 320, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <Text type="secondary">{t("preview.selectMetricPrompt")}</Text>
        </div>
      )}
    </div>
  );
}

function NowFiringPill({
  isLoading,
  isError,
  error,
  seriesCount,
  enabled,
}: {
  isLoading: boolean;
  isError: boolean;
  error: unknown;
  seriesCount: number | undefined;
  enabled: boolean;
}) {
  const { t } = useI18n();
  if (!enabled) return null;
  if (isLoading) {
    return (
      <Space>
        <Text type="secondary">{t("preview.nowFiringLabel")}</Text>
        <Tag>{t("preview.checking")}</Tag>
      </Space>
    );
  }
  if (isError) {
    return (
      <Space>
        <Text type="secondary">{t("preview.nowFiringLabel")}</Text>
        <Tag title={errorMessage(error, t("preview.checkFailed"))}>{t("preview.checkUnavailable")}</Tag>
      </Space>
    );
  }
  const count = seriesCount ?? 0;
  return (
    <Space>
      <Text type="secondary">{t("preview.nowFiringLabel")}</Text>
      <Tag color={count > 0 ? "red" : "green"}>
        {count > 0 ? t("preview.firingCount", { count }) : t("preview.notFiring")}
      </Tag>
    </Space>
  );
}
