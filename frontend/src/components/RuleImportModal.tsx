import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Divider,
  Input,
  List,
  Modal,
  Radio,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  Upload,
} from "antd";
import type { UploadProps } from "antd";
import { ApiError } from "../api/client";
import { listClusters } from "../api/admin";
import { importRules } from "../api/rules";
import type {
  ConflictStrategy,
  ImportAction,
  RuleExportEnvelope,
  RuleImportResult,
  RuleImportVerdict,
} from "../api/rules";

const { Text, Paragraph } = Typography;
const { Dragger } = Upload;

const STRATEGY_OPTIONS: { value: ConflictStrategy; label: string }[] = [
  { value: "skip", label: "건너뛰기" },
  { value: "overwrite", label: "덮어쓰기" },
  { value: "rename", label: "이름 변경" },
];

const ACTION_TAG: Record<ImportAction, { color: string; label: string }> = {
  created: { color: "green", label: "생성" },
  renamed: { color: "blue", label: "이름 변경됨" },
  skipped: { color: "default", label: "건너뜀" },
  overwritten: { color: "orange", label: "덮어씀" },
  failed: { color: "red", label: "실패" },
};

function parseEnvelope(text: string): { data: RuleExportEnvelope | null; error: string | null } {
  if (!text.trim()) return { data: null, error: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { data: null, error: "올바른 JSON이 아닙니다" };
  }
  const data = parsed as Partial<RuleExportEnvelope>;
  if (data.kam_export_version !== 1 || data.kind !== "rules" || !Array.isArray(data.rules)) {
    return {
      data: null,
      error: '지원하지 않는 형식입니다 (kam_export_version: 1, kind: "rules"가 필요합니다)',
    };
  }
  return { data: data as RuleExportEnvelope, error: null };
}

function SummaryTags({ summary }: { summary: RuleImportResult["summary"] }) {
  return (
    <Space wrap>
      <Tag color="green">생성 {summary.created}</Tag>
      <Tag color="blue">이름 변경 {summary.renamed}</Tag>
      <Tag color="orange">덮어씀 {summary.overwritten}</Tag>
      <Tag>건너뜀 {summary.skipped}</Tag>
      <Tag color="red">실패 {summary.failed}</Tag>
    </Space>
  );
}

function VerdictTable({ verdicts }: { verdicts: RuleImportVerdict[] }) {
  return (
    <Table<RuleImportVerdict>
      size="small"
      rowKey="slug"
      dataSource={verdicts}
      pagination={false}
      style={{ marginTop: 12 }}
      columns={[
        { title: "슬러그", dataIndex: "slug", key: "slug" },
        {
          title: "액션",
          dataIndex: "action",
          key: "action",
          render: (action: ImportAction) => (
            <Tag color={ACTION_TAG[action].color}>{ACTION_TAG[action].label}</Tag>
          ),
        },
        {
          title: "최종 슬러그",
          dataIndex: "final_slug",
          key: "final_slug",
          render: (value: string | null) => value ?? <Text type="secondary">-</Text>,
        },
      ]}
      expandable={{
        rowExpandable: (record) => record.errors.length > 0 || record.warnings.length > 0,
        expandedRowRender: (record) => (
          <>
            {record.errors.length > 0 && (
              <List
                size="small"
                header={<Text type="danger">오류</Text>}
                dataSource={record.errors}
                renderItem={(item) => <List.Item>{item}</List.Item>}
              />
            )}
            {record.warnings.length > 0 && (
              <List
                size="small"
                header={<Text type="warning">경고</Text>}
                dataSource={record.warnings}
                renderItem={(item) => <List.Item>{item}</List.Item>}
              />
            )}
          </>
        ),
      }}
    />
  );
}

interface RuleImportModalProps {
  open: boolean;
  onClose: () => void;
  teamId: number;
}

export default function RuleImportModal({ open, onClose, teamId }: RuleImportModalProps) {
  const queryClient = useQueryClient();
  const [envelopeText, setEnvelopeText] = useState("");
  const [targetClusterId, setTargetClusterId] = useState<number | undefined>(undefined);
  const [conflictStrategy, setConflictStrategy] = useState<ConflictStrategy>("skip");
  const [applyResult, setApplyResult] = useState<RuleImportResult | null>(null);

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const { data: envelope, error: parseError } = useMemo(
    () => parseEnvelope(envelopeText),
    [envelopeText],
  );

  const canPreview = envelope !== null && targetClusterId !== undefined;

  // A dry-run preview runs automatically the moment a valid file/paste,
  // target cluster, and strategy are all chosen -- and re-runs whenever any
  // of those three change, so what's on screen is always the plan for
  // *this* combination rather than a stale one from an earlier choice.
  const previewQuery = useQuery({
    queryKey: ["rule-import-preview", teamId, targetClusterId, conflictStrategy, envelope],
    queryFn: () =>
      importRules(teamId, {
        data: envelope!,
        targetClusterId: targetClusterId!,
        conflictStrategy,
        dryRun: true,
      }),
    enabled: canPreview,
  });

  const applyMutation = useMutation({
    mutationFn: () =>
      importRules(teamId, {
        data: envelope!,
        targetClusterId: targetClusterId!,
        conflictStrategy,
        dryRun: false,
      }),
    onSuccess: (result) => {
      setApplyResult(result);
      queryClient.invalidateQueries({ queryKey: ["rules", teamId, targetClusterId] });
    },
  });

  const reset = () => {
    setEnvelopeText("");
    setTargetClusterId(undefined);
    setConflictStrategy("skip");
    setApplyResult(null);
    applyMutation.reset();
  };

  const handleClose = () => {
    reset();
    onClose();
  };

  const uploadProps: UploadProps = {
    accept: ".json",
    maxCount: 1,
    showUploadList: false,
    beforeUpload: (file) => {
      file.text().then(setEnvelopeText);
      return false;
    },
  };

  return (
    <Modal
      title="룰 가져오기"
      open={open}
      onCancel={handleClose}
      width={720}
      destroyOnClose
      footer={
        applyResult
          ? [
              <Button key="close" type="primary" onClick={handleClose}>
                닫기
              </Button>,
            ]
          : [
              <Button key="cancel" onClick={handleClose}>
                취소
              </Button>,
              <Button
                key="apply"
                type="primary"
                disabled={!previewQuery.isSuccess}
                loading={applyMutation.isPending}
                onClick={() => applyMutation.mutate()}
              >
                적용
              </Button>,
            ]
      }
    >
      {applyResult ? (
        <div>
          <Alert
            type="success"
            showIcon
            style={{ marginBottom: 16 }}
            message="가져오기를 적용했습니다"
          />
          <SummaryTags summary={applyResult.summary} />
          <VerdictTable verdicts={applyResult.verdicts} />
        </div>
      ) : (
        <div>
          <Dragger {...uploadProps} style={{ marginBottom: 12 }}>
            <p className="ant-upload-text">내보내기 JSON 파일을 끌어다 놓거나 클릭해서 선택하세요</p>
          </Dragger>

          <Divider plain>또는 직접 붙여넣기</Divider>

          <Input.TextArea
            rows={4}
            placeholder="내보내기 JSON을 붙여넣으세요"
            value={envelopeText}
            onChange={(e) => setEnvelopeText(e.target.value)}
            style={{ fontFamily: "monospace", marginBottom: 12 }}
          />

          {parseError && (
            <Alert type="error" showIcon message={parseError} style={{ marginBottom: 12 }} />
          )}

          <Space direction="vertical" style={{ width: "100%", marginBottom: 12 }}>
            <div>
              <Paragraph style={{ marginBottom: 4 }}>
                <Text strong>대상 클러스터</Text>
              </Paragraph>
              <Select
                style={{ width: "100%" }}
                placeholder="클러스터를 선택하세요"
                loading={clustersQuery.isLoading}
                value={targetClusterId}
                onChange={setTargetClusterId}
                options={(clustersQuery.data ?? []).map((c) => ({
                  value: c.id,
                  label: c.display_name,
                }))}
              />
            </div>
            <div>
              <Paragraph style={{ marginBottom: 4 }}>
                <Text strong>충돌 처리 전략</Text>
              </Paragraph>
              <Radio.Group
                options={STRATEGY_OPTIONS}
                optionType="button"
                value={conflictStrategy}
                onChange={(e) => setConflictStrategy(e.target.value as ConflictStrategy)}
              />
            </div>
          </Space>

          {canPreview && (
            <div>
              <Divider plain>미리보기 (dry-run)</Divider>
              {previewQuery.isLoading && <Text type="secondary">미리보기를 계산하는 중...</Text>}
              {previewQuery.isError && (
                <Alert
                  type="error"
                  showIcon
                  message={
                    previewQuery.error instanceof ApiError
                      ? previewQuery.error.detail
                      : "미리보기 계산에 실패했습니다"
                  }
                />
              )}
              {previewQuery.data && (
                <>
                  <SummaryTags summary={previewQuery.data.summary} />
                  <VerdictTable verdicts={previewQuery.data.verdicts} />
                </>
              )}
            </div>
          )}

          {applyMutation.isError && (
            <Alert
              type="error"
              showIcon
              style={{ marginTop: 12 }}
              message={
                applyMutation.error instanceof ApiError
                  ? applyMutation.error.detail
                  : "가져오기 적용에 실패했습니다"
              }
            />
          )}
        </div>
      )}
    </Modal>
  );
}
