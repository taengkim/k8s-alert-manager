import type { CSSProperties } from "react";
import type { ThemeConfig } from "antd";

/**
 * "밝은 관제실(bright NOC)" design tokens -- the single source of truth for
 * color/typography across the app. Import from here rather than
 * re-declaring hex values in a page.
 */
export const palette = {
  paper: "#F6F7F9", // page background -- cool gray
  surface: "#FFFFFF", // cards/tables/sider
  hairline: "#E4E8EE", // borders -- hairlines instead of shadows
  ink: "#1C2B36", // body text (blue-black)
  inkMuted: "#5B6B79",
  primary: "#0F6E73", // Harbor Teal -- deliberately not antd's default blue
} as const;

/** Semantic only -- reserved for representing alert/heartbeat severity.
 * Never reuse these for unrelated emphasis (e.g. a plain "important" red). */
export const severity = {
  critical: "#CF1322",
  warning: "#D46B08",
  info: "#3E6B9E",
  none: "#8C9BAB",
  ok: "#2F9E44", // resolved / healthy
} as const;

export type SeverityKey = keyof typeof severity;

/** CVD/contrast-validated categorical palette for charts with a genuine
 * multi-series dimension. Fixed assignment order -- never reorder or cycle;
 * a 6th series folds into "기타" rather than reusing index 0. */
export const chartCategorical = ["#00929E", "#5A63D8", "#C77D1C", "#B0459B", "#55842B"] as const;

export const fontFamily =
  "'IBM Plex Sans KR', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
/** Limited to genuinely code-like data: PromQL/expr, fingerprints, label and
 * matcher key=value chips, metric-name autocomplete. Never timestamps. */
export const monoFontFamily =
  "'IBM Plex Mono', ui-monospace, SFMono-Regular, Menlo, monospace";

export function withAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/**
 * Maps a severity value (as it appears on alerts/rules/routes -- "critical"
 * | "warning" | "info", or unset/"none"/null) to this design's semantic
 * color -- one mapping everywhere severity is shown (tags, dots, charts).
 */
export function severityColor(sev: string | null | undefined): string {
  switch (sev) {
    case "critical":
      return severity.critical;
    case "warning":
      return severity.warning;
    case "info":
      return severity.info;
    case "ok":
    case "resolved":
      return severity.ok;
    default:
      return severity.none;
  }
}

/** GET /clusters' heartbeat_state -> the same severity language ("missing"
 * reads as critical, "late" as a warning, everything else as neutral). */
export function heartbeatColor(state: string | null | undefined): string {
  switch (state) {
    case "missing":
      return severity.critical;
    case "late":
      return severity.warning;
    case "ok":
      return severity.ok;
    default:
      return severity.none;
  }
}

/** Tinted (not filled) severity Tag: colored text on a faint wash of the
 * same hue, so a table of tags reads as a scannable column instead of a
 * row of solid chips. Always paired with a text label -- color is never
 * the only encoding. Use as <Tag style={severityTagStyle(sev)}>. */
export function severityTagStyle(sev: string | null | undefined): CSSProperties {
  const color = severityColor(sev);
  return {
    color,
    background: withAlpha(color, 0.07),
    borderColor: withAlpha(color, 0.35),
  };
}

export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: palette.primary,
    colorInfo: palette.primary,
    colorLink: palette.primary,
    colorBgLayout: palette.paper,
    colorBgContainer: palette.surface,
    colorText: palette.ink,
    colorTextSecondary: palette.inkMuted,
    colorTextTertiary: palette.inkMuted,
    colorBorder: palette.hairline,
    colorBorderSecondary: palette.hairline,
    borderRadius: 6,
    borderRadiusLG: 10,
    borderRadiusSM: 4,
    fontFamily,
    fontSize: 14,
    controlHeight: 34,
  },
  components: {
    Layout: {
      siderBg: palette.surface,
      headerBg: palette.surface,
      headerHeight: 52,
      bodyBg: palette.paper,
    },
    Menu: {
      itemHeight: 40,
      itemBorderRadius: 6,
      itemColor: palette.ink,
      itemHoverColor: palette.primary,
      itemHoverBg: withAlpha(palette.primary, 0.04),
      itemSelectedColor: palette.primary,
      itemSelectedBg: withAlpha(palette.primary, 0.06),
      activeBarBorderWidth: 0,
    },
    Table: {
      headerBg: palette.paper,
      headerColor: palette.inkMuted,
      fontSize: 13,
      cellPaddingBlock: 10,
      cellPaddingInline: 12,
    },
    Card: {
      // Hairline borders carry the separation; shadows stay on overlays
      // (Modal/Drawer/Dropdown) only.
      boxShadowTertiary: "none",
    },
  },
};
