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

/**
 * Parse JS's own `toString()` output for a non-zero number -- always
 * either plain decimal ("123.456", "0.0001") or scientific ("1e-5",
 * "1e+21") -- into (sign, digits, exp) such that
 * value == sign + digits[0] + "." + digits.slice(1) + "e" + exp, i.e.
 * value = (sign)D.DDD * 10**exp where digits has no leading or trailing
 * zeros. Mirrors the backend's `_parse_native_float_repr` exactly: this is
 * purely string manipulation on toString()'s own already-correct
 * shortest-round-trip digit sequence, never a numeric re-derivation (e.g.
 * via log10, which would risk off-by-one errors from floating-point
 * imprecision at exact power-of-ten boundaries).
 */
function parseNativeNumberString(s: string): { sign: string; digits: string; exp: number } {
  let sign = "";
  if (s.startsWith("-")) {
    sign = "-";
    s = s.slice(1);
  }
  let mantissa = s;
  let sciExp = 0;
  const eIndex = s.indexOf("e");
  if (eIndex !== -1) {
    mantissa = s.slice(0, eIndex);
    sciExp = parseInt(s.slice(eIndex + 1), 10);
  }
  const dotIndex = mantissa.indexOf(".");
  const intPart = dotIndex === -1 ? mantissa : mantissa.slice(0, dotIndex);
  const fracPart = dotIndex === -1 ? "" : mantissa.slice(dotIndex + 1);
  const combined = intPart + fracPart;
  const dotPos = intPart.length;
  const firstNonZero = combined.split("").findIndex((c) => c !== "0");
  if (firstNonZero === -1) {
    return { sign, digits: "0", exp: 0 };
  }
  const digits = combined.slice(firstNonZero).replace(/0+$/, "") || "0";
  const exp = dotPos - firstNonZero - 1 + sciExp;
  return { sign, digits, exp };
}

/** Inverse of `parseNativeNumberString`, applying OUR OWN canonical
 * fixed/scientific threshold and exponent format rather than JS's or
 * Python's native (and mutually divergent) ones -- see `formatPromqlNumber`. */
function renderNormalizedNumber(sign: string, digits: string, exp: number): string {
  if (digits === "0") return "0";
  if (exp >= -4 && exp < 21) {
    let intPart: string;
    let fracPart: string;
    if (exp >= 0) {
      if (digits.length <= exp + 1) {
        intPart = digits + "0".repeat(exp + 1 - digits.length);
        fracPart = "";
      } else {
        intPart = digits.slice(0, exp + 1);
        fracPart = digits.slice(exp + 1);
      }
    } else {
      intPart = "0";
      fracPart = "0".repeat(-exp - 1) + digits;
    }
    return sign + intPart + (fracPart ? "." + fracPart : "");
  }
  const mantissa = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits[0];
  const expSign = exp < 0 ? "-" : "+";
  return `${sign}${mantissa}e${expSign}${Math.abs(exp)}`;
}

/**
 * Render `value` in a format PINNED to match the backend's
 * `_format_promql_number` (app/services/rules.py) byte-for-byte, since
 * this is compared directly against the server-recomputed expression when
 * deciding whether a saved rule is still in builder mode.
 *
 * This can't just be `${value}`/`String(value)` vs. Python's `repr()`:
 * those two natively disagree both on WHEN to switch from fixed to
 * scientific notation (JS flips around 1e-6/1e21, Python around
 * 1e-4/1e16) and on how they zero-pad the exponent (JS: "1e-5", Python:
 * "1e-05") -- e.g. 0.00001 stays "0.00001" in JS but round-trips as
 * "1e-05" in Python, and 1e-7 is "1e-7" vs "1e-07".
 *
 * The fix: parse each language's own native shortest-round-trip string
 * (toString()/repr()) into (sign, digits, exponent) -- see
 * `parseNativeNumberString` -- then re-render with OUR OWN rule, applied
 * identically on both sides: fixed notation for -4 <= exponent < 21
 * (trimmed, no trailing zeros), scientific otherwise with an unpadded,
 * explicitly-signed exponent (e.g. "1e-5", "1e+21"). Both sides only ever
 * reformat the exact digit sequence their own native shortest-round-trip
 * algorithm already produced, so the output is guaranteed to agree
 * without re-deriving anything numerically.
 */
function formatPromqlNumber(value: number): string {
  if (value === 0) return "0";
  if (!Number.isFinite(value)) return String(value);
  const { sign, digits, exp } = parseNativeNumberString(value.toString());
  return renderNormalizedNumber(sign, digits, exp);
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
