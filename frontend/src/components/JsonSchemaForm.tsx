/**
 * Renders antd Form.Item fields for a JSON Schema object's `properties`,
 * to be used inside an existing <Form> (e.g. the channel create/edit
 * modal) so a channel type's config -- built-in or third-party plugin --
 * gets a form with zero per-type frontend code.
 *
 * This is NOT a general JSON Schema renderer. Supported subset only:
 * string -> Input, number/integer -> InputNumber, boolean -> Switch,
 * array of string -> Select mode="tags", string/number enum -> Select.
 * `required` is reflected as a form rule. Anything else (nested objects,
 * oneOf/anyOf, tuples, ...) falls back to a raw JSON textarea so the field
 * is still editable, just without type-specific input affordances.
 */

import { Form, Input, InputNumber, Select, Switch } from "antd";
import type { JsonSchemaObject, JsonSchemaProperty } from "../api/channels";
import { useI18n } from "../i18n";

interface JsonSchemaFormProps {
  schema: JsonSchemaObject;
  /** Field path each property nests under -- values land at
   * `values.<...namePrefix>.<key>` in the parent Form. */
  namePrefix?: (string | number)[];
}

const INVALID_JSON_MARKER = "__jsonSchemaFormInvalidJson";

interface InvalidJsonValue {
  [INVALID_JSON_MARKER]: true;
  raw: string;
}

function isInvalidJsonValue(value: unknown): value is InvalidJsonValue {
  return typeof value === "object" && value !== null && INVALID_JSON_MARKER in value;
}

/** Used as both `normalize` (textarea input -> stored form value) and to
 * detect a parse failure at validation time -- a failed parse is kept as a
 * tagged object (instead of throwing) so the raw text isn't lost from the
 * field while the user fixes it.
 */
function parseJsonFallback(raw: string): unknown {
  if (raw.trim() === "") return undefined;
  try {
    return JSON.parse(raw);
  } catch {
    return { [INVALID_JSON_MARKER]: true, raw } satisfies InvalidJsonValue;
  }
}

function scalarType(prop: JsonSchemaProperty): string | undefined {
  if (Array.isArray(prop.type)) {
    return prop.type.find((t) => t !== "null");
  }
  return prop.type;
}

export default function JsonSchemaForm({ schema, namePrefix = ["config"] }: JsonSchemaFormProps) {
  const { t } = useI18n();
  const required = new Set(schema.required ?? []);

  return (
    <>
      {Object.entries(schema.properties).map(([key, prop]) => {
        const name = [...namePrefix, key];
        const label = prop.title ?? key;
        const isRequired = required.has(key);
        const type = scalarType(prop);

        if (type === "boolean") {
          return (
            <Form.Item
              key={key}
              name={name}
              label={label}
              valuePropName="checked"
              initialValue={prop.default ?? false}
              tooltip={prop.description}
            >
              <Switch />
            </Form.Item>
          );
        }

        if (type === "integer" || type === "number") {
          return (
            <Form.Item
              key={key}
              name={name}
              label={label}
              initialValue={prop.default}
              tooltip={prop.description}
              rules={[{ required: isRequired, message: t("schemaForm.required", { label }) }]}
            >
              <InputNumber style={{ width: "100%" }} />
            </Form.Item>
          );
        }

        if (prop.enum && prop.enum.length > 0) {
          return (
            <Form.Item
              key={key}
              name={name}
              label={label}
              initialValue={prop.default}
              tooltip={prop.description}
              rules={[{ required: isRequired, message: t("schemaForm.selectRequired", { label }) }]}
            >
              <Select options={prop.enum.map((v) => ({ value: v, label: String(v) }))} />
            </Form.Item>
          );
        }

        if (type === "array" && prop.items && scalarType(prop.items) === "string") {
          const minItems = prop.minItems;
          return (
            <Form.Item
              key={key}
              name={name}
              label={label}
              initialValue={prop.default}
              tooltip={prop.description}
              rules={[
                { required: isRequired, message: t("schemaForm.required", { label }) },
                {
                  validator: async (_, value?: unknown[]) => {
                    if (minItems && (!value || value.length < minItems)) {
                      throw new Error(t("schemaForm.minItems", { label, min: minItems }));
                    }
                  },
                },
              ]}
            >
              <Select mode="tags" tokenSeparators={[",", " "]} />
            </Form.Item>
          );
        }

        if (type === "string") {
          return (
            <Form.Item
              key={key}
              name={name}
              label={label}
              initialValue={prop.default}
              tooltip={prop.description}
              rules={[{ required: isRequired, message: t("schemaForm.required", { label }) }]}
            >
              <Input placeholder={prop.format} />
            </Form.Item>
          );
        }

        // Unsupported type (nested object, oneOf/anyOf, tuple, ...): still
        // editable, just as raw JSON rather than a typed widget.
        return (
          <Form.Item
            key={key}
            name={name}
            label={label}
            tooltip={prop.description}
            extra={t("schemaForm.unsupportedFieldHelp")}
            getValueProps={(value) => ({
              value: isInvalidJsonValue(value)
                ? value.raw
                : value === undefined
                  ? ""
                  : JSON.stringify(value, null, 2),
            })}
            normalize={parseJsonFallback}
            rules={[
              { required: isRequired, message: t("schemaForm.required", { label }) },
              {
                validator: async (_, value) => {
                  if (isInvalidJsonValue(value)) {
                    throw new Error(t("ruleImport.invalidJson"));
                  }
                },
              },
            ]}
          >
            <Input.TextArea rows={4} />
          </Form.Item>
        );
      })}
    </>
  );
}
