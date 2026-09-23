/**
 * The threshold builder's guided-authoring state, and the canonical PromQL
 * generator for it.
 *
 * The generator's output format is pinned and MUST match the backend's
 * `generate_builder_expr` (app/services/rules.py) byte-for-byte: no spaces
 * inside the label-matcher braces, exactly one space on each side of the
 * comparison operator. The backend recomputes this same expression from a
 * saved rule's `kam.io/builder-v1` annotation and compares it against the
 * stored expr to decide whether the rule is still "in sync" with its
 * builder state (mode=builder) or was hand-edited afterwards (mode=promql)
 * -- if this drifts from the backend's formatting, every rule saved via the
 * builder would come back reporting mode=promql on reload.
 */

export type LabelOp = "=" | "!=" | "=~" | "!~";
export type ComparisonOp = ">" | ">=" | "<" | "<=" | "==" | "!=";

export interface BuilderLabelFilter {
  key: string;
  op: LabelOp;
  value: string;
}

export interface BuilderState {
  metric: string;
  labels: BuilderLabelFilter[];
  comparison: ComparisonOp;
  threshold: number;
}

export const LABEL_OPS: LabelOp[] = ["=", "!=", "=~", "!~"];
export const COMPARISON_OPS: ComparisonOp[] = [">", ">=", "<", "<=", "==", "!="];

export function emptyBuilderState(): BuilderState {
  return { metric: "", labels: [], comparison: ">", threshold: 0 };
}

/** Matches the backend's `_format_promql_number`: JS's own `${value}`
 * template interpolation already renders 5.0 as "5" and 1.5 as "1.5", so
 * this is just String(value) -- kept as a named function for symmetry with
 * the backend and to make the shared-format contract explicit at the call
 * site. */
function formatPromqlNumber(value: number): string {
  return String(value);
}

function quotePromqlString(value: string): string {
  return value.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/** The metric+label selector alone, with no comparison -- what the preview
 * chart queries (so it plots the metric's actual values, not a 0/1 boolean
 * from a comparison against the threshold). */
export function generateSelector(state: BuilderState): string {
  const filters = state.labels.filter((l) => l.key);
  if (filters.length === 0) return state.metric;
  const matchers = filters
    .map((l) => `${l.key}${l.op}"${quotePromqlString(l.value)}"`)
    .join(",");
  return `${state.metric}{${matchers}}`;
}

/** The full alerting expression (selector + comparison + threshold) --
 * what gets saved as the rule's expr and what "지금 발생?" queries against. */
export function generateBuilderExpr(state: BuilderState): string {
  return `${generateSelector(state)} ${state.comparison} ${formatPromqlNumber(state.threshold)}`;
}
