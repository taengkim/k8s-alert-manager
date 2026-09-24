import { Button, Form, Input, Segmented, Select, Space } from "antd";
import type { FormInstance } from "antd";
import type { NamePath } from "antd/es/form/interface";
import type { MatcherKind, MatcherTarget } from "../api/routes";
import { useI18n } from "../i18n";

function isValidRegex(pattern: string): boolean {
  try {
    // eslint-disable-next-line no-new
    new RegExp(pattern);
    return true;
  } catch {
    return false;
  }
}

interface MatcherListEditorProps {
  /** The Form.List field this editor's matchers live under -- "matchers"
   * for RouteEditor's top-level form, or a nested path (e.g.
   * ["matchers"] within a share-create modal's own form) wherever it's
   * embedded. */
  name: NamePath;
  form: FormInstance;
}

/** The include/exclude matcher builder shared by RouteEditor (a routing
 * rule's own matchers) and Shares.tsx's create/edit share modal (an
 * AlertShare's scope matchers) -- both are the same
 * {kind, target, key?, pattern} shape, evaluated by the same backend
 * semantics (see app/services/routing.py's compile_matchers), so the UI to
 * build one is extracted here instead of duplicated between the two pages.
 */
export default function MatcherListEditor({ name, form }: MatcherListEditorProps) {
  const { t } = useI18n();
  return (
    <Form.List name={name}>
      {(fields, { add, remove }) => (
        <>
          {fields.map((field) => (
            <MatcherRow
              key={field.key}
              field={field}
              form={form}
              listName={name}
              onRemove={() => remove(field.name)}
            />
          ))}
          <Button
            onClick={() => add({ kind: "include", target: "alertname", pattern: "" })}
            style={{ marginBottom: 16 }}
          >
            {t("matcher.addMatcher")}
          </Button>
        </>
      )}
    </Form.List>
  );
}

function MatcherRow({
  field,
  form,
  listName,
  onRemove,
}: {
  field: { key: number; name: number };
  form: FormInstance;
  listName: NamePath;
  onRemove: () => void;
}) {
  const { t } = useI18n();
  const MATCHER_KIND_OPTIONS: { value: MatcherKind; label: string }[] = [
    { value: "include", label: t("matcher.kindInclude") },
    { value: "exclude", label: t("matcher.kindExclude") },
  ];
  const MATCHER_TARGET_OPTIONS: { value: MatcherTarget; label: string }[] = [
    { value: "alertname", label: t("alerts.alertName") },
    { value: "label", label: t("matcher.targetLabel") },
    { value: "annotation", label: t("matcher.targetAnnotation") },
  ];
  const path = Array.isArray(listName) ? listName : [listName];
  const target = Form.useWatch([...path, field.name, "target"], form) as
    | MatcherTarget
    | undefined;
  const needsKey = target === "label" || target === "annotation";

  return (
    <Space align="baseline" style={{ display: "flex", marginBottom: 8, flexWrap: "wrap" }}>
      <Form.Item name={[field.name, "kind"]} initialValue="include" noStyle>
        <Segmented options={MATCHER_KIND_OPTIONS} />
      </Form.Item>
      <Form.Item name={[field.name, "target"]} initialValue="alertname" noStyle>
        <Select style={{ width: 140 }} options={MATCHER_TARGET_OPTIONS} />
      </Form.Item>
      {needsKey && (
        <Form.Item
          name={[field.name, "key"]}
          rules={[{ required: true, message: t("ruleEditor.keyRequired") }]}
          noStyle
        >
          <Input placeholder="key" style={{ width: 140 }} />
        </Form.Item>
      )}
      <Form.Item
        name={[field.name, "pattern"]}
        rules={[
          { required: true, message: t("matcher.patternRequired") },
          {
            validator: async (_, value?: string) => {
              if (value && !isValidRegex(value)) {
                throw new Error(t("matcher.invalidRegex"));
              }
            },
          },
        ]}
        noStyle
      >
        <Input placeholder={t("matcher.patternPlaceholder")} style={{ width: 220 }} />
      </Form.Item>
      <Button danger onClick={onRemove}>
        {t("common.delete")}
      </Button>
    </Space>
  );
}
