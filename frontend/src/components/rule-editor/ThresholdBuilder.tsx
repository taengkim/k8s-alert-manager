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
import { useI18n } from "../../i18n";

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
  const { t } = useI18n();
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
        // This form lives inside RuleEditor's own <Form>; component={false}
        // keeps the antd FormInstance context (validation, Form.List, etc.)
        // without rendering a second, nested <form> DOM element.
        component={false}
      >
        <Form.Item label={t("builder.metricLabel")} name="metric" rules={[{ required: true, message: t("builder.metricRequired") }]}>
          <AutoComplete
            options={metricOptions}
            onSearch={handleMetricSearch}
            onSelect={(metric: string) => void handleMetricSelect(metric)}
            placeholder={t("builder.metricSearchPlaceholder")}
            filterOption={false}
            notFoundContent={metricSearching ? t("builder.searching") : undefined}
          />
        </Form.Item>
        {metricMeta && (metricMeta.type || metricMeta.help) && (
          <Text type="secondary" style={{ display: "block", marginTop: -16, marginBottom: 16 }}>
            {[metricMeta.type, truncateHelp(metricMeta.help)].filter(Boolean).join(" · ")}
          </Text>
        )}

        <Text strong>{t("builder.labelFilterTitle")}</Text>
        <Form.List name="labels">
          {(fields, { add, remove }) => (
            <div style={{ marginTop: 8, marginBottom: 16 }}>
              {fields.map((field) => (
                <Space key={field.key} align="baseline" style={{ display: "flex", marginBottom: 8 }}>
                  <Form.Item
                    name={[field.name, "key"]}
                    rules={[{ required: true, message: t("builder.labelKeyRequired") }]}
                    noStyle
                  >
                    <Select
                      style={{ width: 160 }}
                      placeholder={t("builder.labelPlaceholder")}
                      showSearch
                      options={labelKeyOptions.map((key) => ({ value: key, label: key }))}
                    />
                  </Form.Item>
                  <Form.Item name={[field.name, "op"]} noStyle initialValue="=">
                    <Select style={{ width: 76 }} options={LABEL_OPS.map((op) => ({ value: op, label: op }))} />
                  </Form.Item>
                  <Form.Item name={[field.name, "value"]} rules={[{ required: true, message: t("builder.valueLabel") }]} noStyle>
                    <LabelValueField clusterId={clusterId} metric={value.metric} label={watchedLabels[field.name]?.key ?? ""} />
                  </Form.Item>
                  <Button danger onClick={() => remove(field.name)}>
                    {t("common.delete")}
                  </Button>
                </Space>
              ))}
              <Button onClick={() => add({ key: "", op: "=", value: "" })}>{t("builder.addLabelFilter")}</Button>
            </div>
          )}
        </Form.List>

        <Space size="middle" style={{ display: "flex", marginBottom: 8 }}>
          <Form.Item label={t("builder.comparisonLabel")} name="comparison" rules={[{ required: true }]}>
            <Select style={{ width: 100 }} options={COMPARISON_OPS.map((op) => ({ value: op, label: op }))} />
          </Form.Item>
          <Form.Item label={t("builder.thresholdLabel")} name="threshold" rules={[{ required: true, message: t("builder.thresholdRequired") }]}>
            <InputNumber style={{ width: 160 }} />
          </Form.Item>
        </Space>
      </Form>

      <Text type="secondary" style={{ display: "block", marginBottom: 4 }}>
        {t("builder.generatedPromqlLabel")}
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
  const { t } = useI18n();
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
      placeholder={t("builder.valueLabel")}
      filterOption={(input, option) =>
        (option?.value ?? "").toLowerCase().includes(input.toLowerCase())
      }
    />
  );
}
