import { useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Dayjs } from "dayjs";
import { Alert, Button, DatePicker, Form, Input, Modal, Select, Switch } from "antd";
import { ApiError } from "../api/client";
import { createSilence } from "../api/silences";
import type { MatcherInput } from "../api/silences";

const DURATION_OPTIONS = [
  { value: "1h", label: "1시간" },
  { value: "4h", label: "4시간" },
  { value: "24h", label: "24시간" },
  { value: "custom", label: "직접 지정" },
];

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
  matchers: MatcherInput[];
  durationPreset: string;
  endsAt?: Dayjs;
  comment: string;
}

interface SilenceModalProps {
  open: boolean;
  onClose: () => void;
  clusterId: number;
  team: SilenceModalTeam;
  /** Prefilled matchers -- e.g. an alert's full label set from the Alerts
   * drawer's "이 알럿 사일런스" button. Defaults to one empty row. */
  initialMatchers?: MatcherInput[];
}

const EMPTY_MATCHER: MatcherInput = { name: "", value: "", is_regex: false };

export default function SilenceModal({
  open,
  onClose,
  clusterId,
  team,
  initialMatchers,
}: SilenceModalProps) {
  const queryClient = useQueryClient();
  const [form] = Form.useForm<SilenceFormValues>();

  // destroyOnClose remounts the Form fresh every open, but initialValues is
  // only read on that first mount -- re-applying it here covers the case
  // where the same already-open modal is fed new prefill matchers (e.g. the
  // Alerts drawer's button re-firing for a different selected alert).
  useEffect(() => {
    if (open) {
      form.setFieldsValue({
        matchers: initialMatchers && initialMatchers.length > 0 ? initialMatchers : [EMPTY_MATCHER],
        durationPreset: "1h",
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialMatchers]);

  const mutation = useMutation({
    mutationFn: (values: SilenceFormValues) => {
      const isCustom = values.durationPreset === "custom";
      return createSilence({
        cluster_id: clusterId,
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
      title="사일런스 생성"
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
              : "사일런스 생성에 실패했습니다"
          }
        />
      )}

      <Form
        form={form}
        layout="vertical"
        initialValues={{
          matchers: initialMatchers && initialMatchers.length > 0 ? initialMatchers : [EMPTY_MATCHER],
          durationPreset: "1h",
        }}
        onFinish={(values) => mutation.mutate(values)}
      >
        <Form.Item label="팀">
          <Input value={`${team.name} (${team.slug})`} disabled />
        </Form.Item>

        <Form.List
          name="matchers"
          rules={[
            {
              validator: async (_, matchers?: MatcherInput[]) => {
                if (!matchers || matchers.length === 0) {
                  throw new Error("matcher를 하나 이상 입력하세요");
                }
              },
            },
          ]}
        >
          {(fields, { add, remove }, { errors }) => (
            <>
              <Form.Item label="Matchers" required>
                {fields.map((field) => (
                  <div
                    key={field.key}
                    style={{ display: "flex", gap: 8, marginBottom: 8, alignItems: "baseline" }}
                  >
                    <Form.Item
                      name={[field.name, "name"]}
                      rules={[{ required: true, message: "레이블명" }]}
                      style={{ flex: 1, marginBottom: 0 }}
                    >
                      <Input placeholder="label" />
                    </Form.Item>
                    <span>=</span>
                    <Form.Item
                      name={[field.name, "value"]}
                      rules={[{ required: true, message: "값" }]}
                      style={{ flex: 1, marginBottom: 0 }}
                    >
                      <Input placeholder="value" />
                    </Form.Item>
                    <Form.Item
                      name={[field.name, "is_regex"]}
                      valuePropName="checked"
                      style={{ marginBottom: 0 }}
                    >
                      <Switch checkedChildren="정규식" unCheckedChildren="정규식" />
                    </Form.Item>
                    <Button
                      type="text"
                      danger
                      disabled={fields.length <= 1}
                      onClick={() => remove(field.name)}
                    >
                      삭제
                    </Button>
                  </div>
                ))}
                <Form.ErrorList errors={errors} />
                <Button block onClick={() => add(EMPTY_MATCHER)}>
                  matcher 추가
                </Button>
              </Form.Item>
            </>
          )}
        </Form.List>

        <Form.Item label="기간" required>
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
                    rules={[{ required: true, message: "만료 시각을 선택하세요" }]}
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
          label="설명"
          rules={[{ required: true, message: "설명을 입력하세요" }]}
        >
          <Input.TextArea rows={2} placeholder="사일런스 사유" />
        </Form.Item>
      </Form>
    </Modal>
  );
}
