import { useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Dayjs } from "dayjs";
import { Alert, Button, DatePicker, Form, Input, Modal, Select, Switch } from "antd";
import { ApiError } from "../api/client";
import { createSilence } from "../api/silences";
import type { MatcherInput } from "../api/silences";
import type { Cluster } from "../api/types";
import { useI18n } from "../i18n";

const DURATION_MINUTES: Record<string, number> = {
  "1h": 60,
  "4h": 240,
  "24h": 1440,
};

interface SilenceModalTeam {
  id: number;
  slug: string;
  name: string;
}

interface SilenceFormValues {
  cluster_id: number;
  matchers: MatcherInput[];
  durationPreset: string;
  endsAt?: Dayjs;
  comment: string;
}

interface SilenceModalProps {
  open: boolean;
  onClose: () => void;
  /** Clusters offered in the cluster Select -- typically the enabled ones. */
  clusters: Cluster[];
  team: SilenceModalTeam;
  /** Prefilled cluster -- e.g. the alert's own cluster from the Alerts
   * drawer's "이 알럿 사일런스" button. Left unselected (required field) when
   * there's no alert context, such as the Silences page's generic "생성"
   * button. */
  initialClusterId?: number;
  /** Prefilled matchers -- e.g. an alert's full label set from the Alerts
   * drawer's "이 알럿 사일런스" button. Defaults to one empty row. */
  initialMatchers?: MatcherInput[];
}

const EMPTY_MATCHER: MatcherInput = { name: "", value: "", is_regex: false };

export default function SilenceModal({
  open,
  onClose,
  clusters,
  team,
  initialClusterId,
  initialMatchers,
}: SilenceModalProps) {
  const { t } = useI18n();
  const DURATION_OPTIONS = [
    { value: "1h", label: t("preview.range1h") },
    { value: "4h", label: t("silenceModal.duration4h") },
    { value: "24h", label: t("preview.range24h") },
    { value: "custom", label: t("silenceModal.durationCustom") },
  ];
  const queryClient = useQueryClient();
  const [form] = Form.useForm<SilenceFormValues>();

  // destroyOnClose remounts the Form fresh every open, but initialValues is
  // only read on that first mount -- re-applying it here covers the case
  // where the same already-open modal is fed new prefill matchers (e.g. the
  // Alerts drawer's button re-firing for a different selected alert).
  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        cluster_id: initialClusterId,
        matchers: initialMatchers && initialMatchers.length > 0 ? initialMatchers : [EMPTY_MATCHER],
        durationPreset: "1h",
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialClusterId, initialMatchers]);

  const mutation = useMutation({
    mutationFn: (values: SilenceFormValues) => {
      const isCustom = values.durationPreset === "custom";
      return createSilence({
        cluster_id: values.cluster_id,
        team_id: team.id,
        matchers: values.matchers,
        comment: values.comment,
        ...(isCustom
          ? { ends_at: values.endsAt!.toISOString() }
          : { duration_minutes: DURATION_MINUTES[values.durationPreset] }),
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["silences"] });
      onClose();
    },
  });

  const handleClose = () => {
    mutation.reset();
    onClose();
  };

  return (
    <Modal
      title={t("silences.createButton")}
      open={open}
      onCancel={handleClose}
      onOk={() => form.submit()}
      confirmLoading={mutation.isPending}
      destroyOnClose
    >
      {mutation.isError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            mutation.error instanceof ApiError
              ? mutation.error.detail
              : t("silenceModal.createError")
          }
        />
      )}

      <Form
        form={form}
        layout="vertical"
        initialValues={{
          cluster_id: initialClusterId,
          matchers: initialMatchers && initialMatchers.length > 0 ? initialMatchers : [EMPTY_MATCHER],
          durationPreset: "1h",
        }}
        onFinish={(values) => mutation.mutate(values)}
      >
        <Form.Item label={t("common.team")}>
          <Input value={`${team.name} (${team.slug})`} disabled />
        </Form.Item>

        <Form.Item
          name="cluster_id"
          label={t("common.cluster")}
          rules={[{ required: true, message: t("ruleEditor.selectClusterPrompt") }]}
        >
          <Select
            placeholder={t("ruleEditor.selectClusterPrompt")}
            options={clusters.map((c) => ({ value: c.id, label: c.display_name }))}
          />
        </Form.Item>

        <Form.List
          name="matchers"
          rules={[
            {
              validator: async (_, matchers?: MatcherInput[]) => {
                if (!matchers || matchers.length === 0) {
                  throw new Error(t("silenceModal.matcherRequired"));
                }
              },
            },
          ]}
        >
          {(fields, { add, remove }, { errors }) => (
            <>
              <Form.Item label={t("silences.matchersColumn")} required>
                {fields.map((field) => (
                  <div
                    key={field.key}
                    style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "baseline" }}
                  >
                    <Form.Item
                      name={[field.name, "name"]}
                      rules={[{ required: true, message: t("silenceModal.matcherNameRequired") }]}
                      style={{ flex: 1, marginBottom: 0 }}
                    >
                      <Input placeholder="label" />
                    </Form.Item>
                    <span>=</span>
                    <Form.Item
                      name={[field.name, "value"]}
                      rules={[{ required: true, message: t("builder.valueLabel") }]}
                      style={{ flex: 1, marginBottom: 0 }}
                    >
                      <Input placeholder="value" />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, "is_regex"]}
                      valuePropName="checked"
                      style={{ marginBottom: 0 }}
                    >
                      <Switch checkedChildren={t("silenceModal.regexToggle")} unCheckedChildren={t("silenceModal.regexToggle")} />
                    </Form.Item>
                    <Button
                      type="text"
                      danger
                      disabled={fields.length <= 1}
                      onClick={() => remove(field.name)}
                    >
                      {t("common.delete")}
                    </Button>
                  </div>
                ))}
                <Form.ErrorList errors={errors} />
                <Button block onClick={() => add(EMPTY_MATCHER)}>
                  {t("matcher.addMatcher")}
                </Button>
              </Form.Item>
            </>
          )}
        </Form.List>

        <Form.Item label={t("silences.durationColumn")} required>
          <div style={{ display: "flex", gap: 8 }}>
            <Form.Item name="durationPreset" noStyle rules={[{ required: true }]}>
              <Select options={DURATION_OPTIONS} style={{ width: 160 }} />
            </Form.Item>
            <Form.Item
              noStyle
              shouldUpdate={(prev, cur) => prev.durationPreset !== cur.durationPreset}
            >
              {({ getFieldValue }) =>
                getFieldValue("durationPreset") === "custom" ? (
                  <Form.Item
                    name="endsAt"
                    noStyle
                    rules={[{ required: true, message: t("silenceModal.endsAtRequired") }]}
                  >
                    <DatePicker showTime style={{ flex: 1 }} />
                  </Form.Item>
                ) : null
              }
            </Form.Item>
          </div>
        </Form.Item>

        <Form.Item
          name="comment"
          label={t("common.description")}
          rules={[{ required: true, message: t("silenceModal.commentRequired") }]}
        >
          <Input.TextArea rows={2} placeholder={t("silenceModal.commentPlaceholder")} />
        </Form.Item>
      </Form>
    </Modal>
  );
}
