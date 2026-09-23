import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import { Alert, Button, Form, Input, Modal, Select, Space, Table, Tag, Typography } from "antd";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { getAlertHistoryNotifications } from "../api/history";
import type { NotificationStatus } from "../api/history";
import { fireTestAlert } from "../api/testAlert";
import type { RouteVerdict } from "../api/routes";
import type { TestAlertInput, TestAlertResult } from "../api/testAlert";

const { Text } = Typography;

const VERDICT_LABEL: Record<RouteVerdict, string> = {
  matched: "일치",
  cluster_filtered: "클러스터 필터링됨",
  gated: "비활성화 / 트리거 불일치",
  severity_filtered: "심각도 필터링됨",
  namespace_filtered: "네임스페이스 필터링됨",
  not_included: "포함 조건 불일치",
  excluded: "제외 조건에 매치",
};

const VERDICT_COLOR: Record<RouteVerdict, string> = {
  matched: "green",
  cluster_filtered: "default",
  gated: "default",
  severity_filtered: "default",
  namespace_filtered: "default",
  not_included: "default",
  excluded: "red",
};

const NOTIFICATION_STATUS_LABEL: Record<NotificationStatus, string> = {
  pending: "대기",
  in_progress: "발송 중",
  delivered: "발송 완료",
  failed: "실패",
  dead: "포기됨",
};

const NOTIFICATION_STATUS_COLOR: Record<NotificationStatus, string> = {
  pending: "default",
  in_progress: "processing",
  delivered: "green",
  failed: "orange",
  dead: "red",
};

interface LabelPair {
  key: string;
  value: string;
}

interface TestAlertFormValues {
  cluster_id: number;
  alertname: string;
  severity: string;
  namespace?: string;
  labels?: LabelPair[];
}

interface TestAlertModalProps {
  open: boolean;
  onClose: () => void;
  teamId: number;
}

export default function TestAlertModal({ open, onClose, teamId }: TestAlertModalProps) {
  const [form] = Form.useForm<TestAlertFormValues>();
  const [result, setResult] = useState<TestAlertResult | null>(null);

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });

  // 5s-interval polling of the fired test event's delivery status, so the
  // operator can watch pending -> delivered without leaving the modal.
  const notificationsQuery = useQuery({
    queryKey: ["alert-history-notifications", result?.event_id],
    queryFn: () => getAlertHistoryNotifications(result!.event_id),
    enabled: result !== null,
    refetchInterval: 5000,
  });

  useEffect(() => {
    if (open) {
      setResult(null);
      form.resetFields();
    }
  }, [open, form]);

  const mutation = useMutation({
    mutationFn: (values: TestAlertFormValues) => {
      const labels: Record<string, string> = {};
      for (const pair of values.labels ?? []) {
        if (pair?.key) labels[pair.key] = pair.value ?? "";
      }
      const body: TestAlertInput = {
        cluster_id: values.cluster_id,
        alertname: values.alertname,
        severity: values.severity,
        namespace: values.namespace || undefined,
        labels,
      };
      return fireTestAlert(teamId, body);
    },
    onSuccess: (data) => setResult(data),
  });

  const handleClose = () => {
    mutation.reset();
    onClose();
  };

  return (
    <Modal
      title="테스트 알럿 발사"
      open={open}
      onCancel={handleClose}
      footer={
        result
          ? [
              <Button key="again" onClick={() => setResult(null)}>
                다시 발사
              </Button>,
              <Button key="close" type="primary" onClick={handleClose}>
                닫기
              </Button>,
            ]
          : [
              <Button key="cancel" onClick={handleClose}>
                취소
              </Button>,
              <Button
                key="submit"
                type="primary"
                loading={mutation.isPending}
                onClick={() => form.submit()}
              >
                발사
              </Button>,
            ]
      }
      destroyOnClose
      width={640}
    >
      {mutation.isError && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message={
            mutation.error instanceof ApiError
              ? mutation.error.detail
              : "테스트 알럿 발사에 실패했습니다"
          }
        />
      )}

      {!result ? (
        <Form
          form={form}
          layout="vertical"
          initialValues={{ alertname: "KamTestAlert", severity: "warning" }}
          onFinish={(values) => mutation.mutate(values)}
        >
          <Form.Item
            name="cluster_id"
            label="클러스터"
            rules={[{ required: true, message: "클러스터를 선택하세요" }]}
          >
            <Select
              loading={clustersQuery.isLoading}
              options={(clustersQuery.data ?? []).map((c) => ({
                value: c.id,
                label: c.display_name,
              }))}
            />
          </Form.Item>
          <Form.Item name="alertname" label="알럿명" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="severity" label="심각도" rules={[{ required: true }]}>
            <Select
              options={[
                { value: "critical", label: "critical" },
                { value: "warning", label: "warning" },
                { value: "info", label: "info" },
              ]}
            />
          </Form.Item>
          <Form.Item name="namespace" label="네임스페이스">
            <Input placeholder="선택 사항" />
          </Form.Item>
          <Form.List name="labels">
            {(fields, { add, remove }) => (
              <Form.Item label="추가 레이블">
                {fields.map((field) => (
                  <div key={field.key} style={{ display: "flex", gap: 8, marginBottom: 8 }}>
                    <Form.Item name={[field.name, "key"]} noStyle>
                      <Input placeholder="key" style={{ flex: 1 }} />
                    </Form.Item>
                    <Form.Item name={[field.name, "value"]} noStyle>
                      <Input placeholder="value" style={{ flex: 1 }} />
                    </Form.Item>
                    <Button type="text" danger onClick={() => remove(field.name)}>
                      삭제
                    </Button>
                  </div>
                ))}
                <Button block onClick={() => add({ key: "", value: "" })}>
                  레이블 추가
                </Button>
              </Form.Item>
            )}
          </Form.List>
        </Form>
      ) : (
        <div>
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
            message={
              <Space>
                테스트 알럿이 발사되었습니다.
                <Link to={`/alerts/history?highlight=${result.event_id}`}>이력에서 보기</Link>
              </Space>
            }
          />

          <Text strong>규칙 평가 결과</Text>
          <Table
            size="small"
            style={{ marginTop: 8, marginBottom: 16 }}
            rowKey="rule_id"
            dataSource={result.verdicts}
            pagination={false}
            locale={{ emptyText: "이 팀에 활성화된 규칙이 없습니다" }}
            columns={[
              { title: "규칙", dataIndex: "rule_name", key: "rule_name" },
              {
                title: "액션",
                dataIndex: "action",
                key: "action",
                render: (action: string) => (
                  <Tag color={action === "suppress" ? "red" : "blue"}>
                    {action === "suppress" ? "차단" : "알림"}
                  </Tag>
                ),
              },
              {
                title: "결과",
                dataIndex: "verdict",
                key: "verdict",
                render: (verdict: RouteVerdict) => (
                  <Tag color={VERDICT_COLOR[verdict]}>{VERDICT_LABEL[verdict]}</Tag>
                ),
              },
            ]}
          />

          <Text strong>적재된 채널</Text>
          <div style={{ margin: "8px 0 16px" }}>
            {result.delivered_channels.length > 0 ? (
              result.delivered_channels.map((name) => (
                <Tag key={name} color="blue">
                  {name}
                </Tag>
              ))
            ) : (
              <Text type="secondary">일치하는 알림 규칙이 없습니다</Text>
            )}
          </div>

          <Text strong>발송 상태 (5초마다 갱신)</Text>
          <Table
            size="small"
            style={{ marginTop: 8 }}
            rowKey="id"
            loading={notificationsQuery.isLoading}
            dataSource={notificationsQuery.data ?? []}
            pagination={false}
            locale={{ emptyText: "발송 대상 채널이 없습니다" }}
            columns={[
              { title: "채널", dataIndex: "channel_name", key: "channel_name" },
              { title: "트리거", dataIndex: "trigger", key: "trigger" },
              {
                title: "상태",
                dataIndex: "status",
                key: "status",
                render: (value: NotificationStatus) => (
                  <Tag color={NOTIFICATION_STATUS_COLOR[value]}>
                    {NOTIFICATION_STATUS_LABEL[value]}
                  </Tag>
                ),
              },
            ]}
          />
        </div>
      )}
    </Modal>
  );
}
