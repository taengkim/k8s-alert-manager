import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import ReactECharts from "echarts-for-react";
import { Alert, Segmented, Space, Tag, Typography } from "antd";
import { ApiError } from "../../api/client";
import { runInstantQuery, runQueryRange } from "../../api/metrics";
import type { RangeSeries } from "../../api/metrics";

const { Text } = Typography;

type RangeKey = "1h" | "6h" | "24h";

const RANGE_OPTIONS: { label: string; value: RangeKey; seconds: number }[] = [
  { label: "1시간", value: "1h", seconds: 3600 },
  { label: "6시간", value: "6h", seconds: 6 * 3600 },
  { label: "24시간", value: "24h", seconds: 24 * 3600 },
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
                data: [{ yAxis: threshold, label: { formatter: `임계값 ${threshold}` } }],
              },
            }
          : {}),
      })),
    };
  }, [series, threshold]);

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
          message={errorMessage(rangeQuery.error, "미리보기 조회에 실패했습니다")}
          style={{ marginBottom: 8 }}
        />
      )}
      {rangeQuery.data?.truncated && omittedCount > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`${omittedCount}개 시리즈 생략됨`}
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
          <Text type="secondary">메트릭을 선택하면 미리보기가 표시됩니다</Text>
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
  if (!enabled) return null;
  if (isLoading) {
    return (
      <Space>
        <Text type="secondary">지금 발생?</Text>
        <Tag>확인 중...</Tag>
      </Space>
    );
  }
  if (isError) {
    return (
      <Space>
        <Text type="secondary">지금 발생?</Text>
        <Tag title={errorMessage(error, "확인 실패")}>확인 불가</Tag>
      </Space>
    );
  }
  const count = seriesCount ?? 0;
  return (
    <Space>
      <Text type="secondary">지금 발생?</Text>
      <Tag color={count > 0 ? "red" : "green"}>{count > 0 ? `발생 중 (${count})` : "미발생"}</Tag>
    </Space>
  );
}
