import { useEffect, useRef, useState } from "react";
import { AutoComplete, Button, Form, InputNumber, Select, Space, Typography } from "antd";
import {
  fetchLabelValues,
  fetchMetricLabels,
  fetchMetricMetadata,
  fetchMetricNames,
} from "../../api/metrics";
import type { MetricMetadata } from "../../api/metrics";
import { COMPARISON_OPS, LABEL_OPS, generateBuilderExpr } from "./builderExpr";
import type { BuilderLabelFilter, BuilderState } from "./builderExpr";

const { Text } = Typography;

const METRIC_SEARCH_DEBOUNCE_MS = 300;

interface ThresholdBuilderProps {
  clusterId: number | undefined;
  value: BuilderState;
  onChange: (state: BuilderState) => void;
}

function truncateHelp(help: string | undefined, max = 80): string {
  if (!help) return "";
  return help.length > max ? `${help.slice(0, max)}…` : help;
}

export default function ThresholdBuilder({ clusterId, value, onChange }: ThresholdBuilderProps) {
  const [form] = Form.useForm<BuilderState>();
  const [metricOptions, setMetricOptions] = useState<{ value: string }[]>([]);
  const [metricSearching, setMetricSearching] = useState(false);
  const [metricMeta, setMetricMeta] = useState<MetricMetadata | null>(null);
  const [labelKeyOptions, setLabelKeyOptions] = useState<string[]>([]);
  const searchTimer = useRef<number | undefined>(undefined);

  // Keep the local form in sync with externally-driven changes (loading an
  // existing rule in edit mode, or re-entering builder mode after a promql
  // round trip) without fighting the form's own onValuesChange updates.
  useEffect(() => {
    form.setFieldsValue(value);
  }, [value, form]);

  useEffect(() => {
    if (!clusterId || !value.metric) {
      setLabelKeyOptions([]);
      return;
    }
    let cancelled = false;
    fetchMetricLabels(clusterId, value.metric)
      .then((res) => {
        if (!cancelled) setLabelKeyOptions(res.labels);
      })
      .catch(() => {
        if (!cancelled) setLabelKeyOptions([]);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clusterId, value.metric]);

  const handleMetricSearch = (search: string) => {
    if (searchTimer.current) window.clearTimeout(searchTimer.current);
    searchTimer.current = window.setTimeout(async () => {
      if (!clusterId) return;
      setMetricSearching(true);
      try {
        const res = await fetchMetricNames(clusterId, search || undefined, 50);
        setMetricOptions(res.names.map((name) => ({ value: name })));
      } catch {
        setMetricOptions([]);
      } finally {
        setMetricSearching(false);
      }
    }, METRIC_SEARCH_DEBOUNCE_MS);
  };

  const handleMetricSelect = async (metric: string) => {
    // A new metric invalidates any label filters chosen against the old
    // one -- their keys likely don't even exist on the new metric.
    const next: BuilderState = { ...value, metric, labels: [] };
    form.setFieldsValue(next);
    onChange(next);
    setMetricMeta(null);
    if (!clusterId) return;
    try {
      setMetricMeta(await fetchMetricMetadata(clusterId, metric));
    } catch {
      setMetricMeta(null);
    }
  };

  const watchedLabels = Form.useWatch<BuilderLabelFilter[]>("labels", form) ?? value.labels;

  return (
    <div>
      <Form<BuilderState>
        form={form}
        layout="vertical"
        initialValues={value}
        onValuesChange={(_, all) => onChange(all)}
      >
        <Form.Item label="메트릭" name="metric" rules={[{ required: true, message: "메트릭을 선택하세요" }]}>
          <AutoComplete
            options={metricOptions}
            onSearch={handleMetricSearch}
            onSelect={(metric: string) => void handleMetricSelect(metric)}
            placeholder="메트릭명 검색 (예: node_load1)"
            filterOption={false}
            notFoundContent={metricSearching ? "검색 중..." : undefined}
          />
        </Form.Item>
        {metricMeta && (metricMeta.type || metricMeta.help) && (
          <Text type="secondary" style={{ display: "block", marginTop: -16, marginBottom: 16 }}>
            {[metricMeta.type, truncateHelp(metricMeta.help)].filter(Boolean).join(" · ")}
          </Text>
        )}

        <Text strong>레이블 필터</Text>
        <Form.List name="labels">
          {(fields, { add, remove }) => (
            <div style={{ marginTop: 8, marginBottom: 16 }}>
              {fields.map((field) => (
                <Space key={field.key} align="baseline" style={{ display: "flex", marginBottom: 8 }}>
                  <Form.Item
                    name={[field.name, "key"]}
                    rules={[{ required: true, message: "레이블 키" }]}
                    noStyle
                  >
                    <Select
                      style={{ width: 160 }}
                      placeholder="레이블"
                      showSearch
                      options={labelKeyOptions.map((key) => ({ value: key, label: key }))}
                    />
                  </Form.Item>
                  <Form.Item name={[field.name, "op"]} noStyle initialValue="=">
                    <Select style={{ width: 76 }} options={LABEL_OPS.map((op) => ({ value: op, label: op }))} />
                  </Form.Item>
                  <Form.Item name={[field.name, "value"]} rules={[{ required: true, message: "값" }]} noStyle>
                    <LabelValueField clusterId={clusterId} metric={value.metric} label={watchedLabels[field.name]?.key ?? ""} />
                  </Form.Item>
                  <Button danger onClick={() => remove(field.name)}>
                    삭제
                  </Button>
                </Space>
              ))}
              <Button onClick={() => add({ key: "", op: "=", value: "" })}>레이블 필터 추가</Button>
            </div>
          )}
        </Form.List>

        <Space size="middle" style={{ display: "flex", marginBottom: 8 }}>
          <Form.Item label="비교 연산자" name="comparison" rules={[{ required: true }]}>
            <Select style={{ width: 100 }} options={COMPARISON_OPS.map((op) => ({ value: op, label: op }))} />
          </Form.Item>
          <Form.Item label="임계값" name="threshold" rules={[{ required: true, message: "임계값을 입력하세요" }]}>
            <InputNumber style={{ width: 160 }} />
          </Form.Item>
        </Space>
      </Form>

      <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
        생성된 PromQL
      </Text>
      <Typography.Paragraph code style={{ whiteSpace: "pre-wrap" }}>
        {generateBuilderExpr(value) || " "}
      </Typography.Paragraph>
    </div>
  );
}

function LabelValueField({
  clusterId,
  metric,
  label,
  value,
  onChange,
}: {
  clusterId: number | undefined;
  metric: string;
  label: string;
  value?: string;
  onChange?: (value: string) => void;
}) {
  const [options, setOptions] = useState<{ value: string }[]>([]);

  useEffect(() => {
    if (!clusterId || !metric || !label) {
      setOptions([]);
      return;
    }
    let cancelled = false;
    fetchLabelValues(clusterId, metric, label)
      .then((res) => {
        if (!cancelled) setOptions(res.values.map((v) => ({ value: v })));
      })
      .catch(() => {
        if (!cancelled) setOptions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [clusterId, metric, label]);

  return (
    <AutoComplete
      style={{ width: 180 }}
      options={options}
      value={value}
      onChange={onChange}
      placeholder="값"
      filterOption={(input, option) =>
        (option?.value ?? "").toLowerCase().includes(input.toLowerCase())
      }
    />
  );
}
