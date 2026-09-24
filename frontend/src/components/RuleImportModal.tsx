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
import { useI18n } from "../i18n";
import type { TranslationKey } from "../i18n";

const { Text, Paragraph } = Typography;
const { Dragger } = Upload;

const ACTION_TAG_KEY: Record<ImportAction, { color: string; labelKey: TranslationKey }> = {
  created: { color: "green", labelKey: "ruleImport.actionCreated" },
  renamed: { color: "blue", labelKey: "ruleImport.actionRenamed" },
  skipped: { color: "default", labelKey: "ruleImport.actionSkipped" },
  overwritten: { color: "orange", labelKey: "ruleImport.actionOverwritten" },
  failed: { color: "red", labelKey: "ruleImport.actionFailed" },
};

function parseEnvelope(
  text: string,
  t: (key: TranslationKey) => string,
): { data: RuleExportEnvelope | null; error: string | null } {
  if (!text.trim()) return { data: null, error: null };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { data: null, error: t("ruleImport.invalidJson") };
  }
  const data = parsed as Partial<RuleExportEnvelope>;
  if (data.kam_export_version !== 1 || data.kind !== "rules" || !Array.isArray(data.rules)) {
    return {
      data: null,
      error: t("ruleImport.unsupportedFormat"),
    };
  }
  return { data: data as RuleExportEnvelope, error: null };
}

function SummaryTags({ summary }: { summary: RuleImportResult["summary"] }) {
  const { t } = useI18n();
  return (
    <Space wrap>
      <Tag color="green">{t("ruleImport.summaryCreated", { count: summary.created })}</Tag>
      <Tag color="blue">{t("ruleImport.summaryRenamed", { count: summary.renamed })}</Tag>
      <Tag color="orange">{t("ruleImport.summaryOverwritten", { count: summary.overwritten })}</Tag>
      <Tag>{t("ruleImport.summarySkipped", { count: summary.skipped })}</Tag>
      <Tag color="red">{t("ruleImport.summaryFailed", { count: summary.failed })}</Tag>
    </Space>
  );
}

function VerdictTable({ verdicts }: { verdicts: RuleImportVerdict[] }) {
  const { t } = useI18n();
  return (
    <Table<RuleImportVerdict>
      size="small"
      rowKey="slug"
      dataSource={verdicts}
      pagination={false}
      style={{ marginTop: 12 }}
      columns={[
        { title: t("rules.slugColumn"), dataIndex: "slug", key: "slug" },
        {
          title: t("ruleImport.actionColumn"),
          dataIndex: "action",
          key: "action",
          render: (action: ImportAction) => (
            <Tag color={ACTION_TAG_KEY[action].color}>{t(ACTION_TAG_KEY[action].labelKey)}</Tag>
          ),
        },
        {
          title: t("ruleImport.finalSlugColumn"),
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
                header={<Text type="danger">{t("ruleImport.errorsHeader")}</Text>}
                dataSource={record.errors}
                renderItem={(item) => <List.Item>{item}</List.Item>}
              />
            )}
            {record.warnings.length > 0 && (
              <List
                size="small"
                header={<Text type="warning">{t("ruleImport.warningsHeader")}</Text>}
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
  const { t } = useI18n();
  const STRATEGY_OPTIONS: { value: ConflictStrategy; label: string }[] = [
    { value: "skip", label: t("ruleImport.strategySkip") },
    { value: "overwrite", label: t("ruleImport.strategyOverwrite") },
    { value: "rename", label: t("ruleImport.strategyRename") },
  ];
  const queryClient = useQueryClient();
  const [envelopeText, setEnvelopeText] = useState("");
  const [targetClusterId, setTargetClusterId] = useState<number | undefined>(undefined);
  const [conflictStrategy, setConflictStrategy] = useState<ConflictStrategy>("skip");
  const [applyResult, setApplyResult] = useState<RuleImportResult | null>(null);

  const clustersQuery = useQuery({ queryKey: ["clusters"], queryFn: listClusters });
  const { data: envelope, error: parseError } = useMemo(
    () => parseEnvelope(envelopeText, t),
    [envelopeText, t],
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
      title={t("ruleImport.modalTitle")}
      open={open}
      onCancel={handleClose}
      width={720}
      destroyOnClose
      footer={
        applyResult
          ? [
              <Button key="close" type="primary" onClick={handleClose}>
                {t("common.close")}
              </Button>,
            ]
          : [
              <Button key="cancel" onClick={handleClose}>
                {t("common.cancel")}
              </Button>,
              <Button
                key="apply"
                type="primary"
                disabled={!previewQuery.isSuccess}
                loading={applyMutation.isPending}
                onClick={() => applyMutation.mutate()}
              >
                {t("ruleImport.applyButton")}
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
            message={t("ruleImport.applySuccess")}
          />
          <SummaryTags summary={applyResult.summary} />
          <VerdictTable verdicts={applyResult.verdicts} />
        </div>
      ) : (
        <div>
          <Dragger {...uploadProps} style={{ marginBottom: 12 }}>
            <p className="ant-upload-text">{t("ruleImport.dropzoneText")}</p>
          </Dragger>

          <Divider plain>{t("ruleImport.orPasteDivider")}</Divider>

          <Input.TextArea
            rows={4}
            placeholder={t("ruleImport.pastePlaceholder")}
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
                <Text strong>{t("ruleImport.targetClusterLabel")}</Text>
              </Paragraph>
              <Select
                style={{ width: "100%" }}
                placeholder={t("ruleEditor.selectClusterPrompt")}
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
                <Text strong>{t("ruleImport.conflictStrategyLabel")}</Text>
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
              <Divider plain>{t("ruleImport.previewDividerLabel")}</Divider>
              {previewQuery.isLoading && <Text type="secondary">{t("ruleImport.previewLoading")}</Text>}
              {previewQuery.isError && (
                <Alert
                  type="error"
                  showIcon
                  message={
                    previewQuery.error instanceof ApiError
                      ? previewQuery.error.detail
                      : t("ruleImport.previewError")
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
                  : t("ruleImport.applyError")
              }
            />
          )}
        </div>
      )}
    </Modal>
  );
}
