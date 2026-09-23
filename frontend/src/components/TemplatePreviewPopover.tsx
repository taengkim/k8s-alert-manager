import { useState } from "react";
import { Button, Popover, Spin, Typography } from "antd";
import { previewTemplate } from "../api/templates";
import type { MessageTemplate } from "../api/templates";

const { Text } = Typography;

/** A small "미리보기" trigger next to a template picker: on click, renders
 * the selected template against a sample alert (POST /templates/preview,
 * use_sample) and shows the result in a popover -- lets a channel/route
 * editor confirm what a template actually produces without leaving the
 * page. Renders nothing if no template is selected.
 */
export default function TemplatePreviewPopover({
  template,
}: {
  template: MessageTemplate | null | undefined;
}) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rendered, setRendered] = useState<{ title: string; body: string } | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!template) return null;

  const handleOpenChange = (next: boolean) => {
    setOpen(next);
    if (!next) return;

    setLoading(true);
    setError(null);
    setRendered(null);
    void previewTemplate({
      title_template: template.title_template,
      body_template: template.body_template,
      body_html_template: template.body_html_template ?? undefined,
      use_sample: true,
    })
      .then((result) => {
        if (result.rendered) {
          setRendered(result.rendered);
        } else {
          setError(result.errors[0]?.message ?? "미리보기에 실패했습니다");
        }
      })
      .catch(() => setError("미리보기에 실패했습니다"))
      .finally(() => setLoading(false));
  };

  return (
    <Popover
      open={open}
      onOpenChange={handleOpenChange}
      trigger="click"
      title={`미리보기 (샘플 알럿) — ${template.name}`}
      content={
        <div style={{ maxWidth: 320 }}>
          {loading && <Spin size="small" />}
          {error && <Text type="danger">{error}</Text>}
          {rendered && (
            <>
              <div style={{ fontWeight: 600 }}>{rendered.title}</div>
              <pre style={{ whiteSpace: "pre-wrap", fontSize: 12, margin: "4px 0 0" }}>
                {rendered.body}
              </pre>
            </>
          )}
        </div>
      }
    >
      <Button size="small" type="link" style={{ paddingLeft: 0 }}>
        미리보기
      </Button>
    </Popover>
  );
}
