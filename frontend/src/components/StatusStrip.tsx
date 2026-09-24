import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import { App as AntApp, Dropdown, Segmented, Space, Switch, Tooltip, Typography } from "antd";
import { useNavigate } from "react-router";
import { useAuth } from "../auth/AuthProvider";
import { useTeam } from "../auth/TeamContext";
import { useClusterFilter } from "../auth/ClusterFilterContext";
import { getLiveAlerts } from "../api/alerts";
import {
  isWebNotifyEnabled,
  setWebNotifyEnabled,
  useAlertStreamStatus,
  type AlertStreamStatus,
} from "../api/useAlertStream";
import { heartbeatColor, palette, severity, severityColor } from "../theme";
import { useI18n, type Lang, type TranslationKey } from "../i18n";
import TeamSwitcher from "./TeamSwitcher";
import ClusterFilterSelect from "./ClusterFilterSelect";

dayjs.extend(relativeTime);

const { Text } = Typography;

const CONNECTION_LABEL_KEY: Record<AlertStreamStatus, TranslationKey> = {
  open: "strip.connOpen",
  connecting: "strip.connConnecting",
  closed: "strip.connClosed",
};

const LIVE_SEVERITIES: { key: "critical" | "warning" | "info"; label: string }[] = [
  { key: "critical", label: "critical" },
  { key: "warning", label: "warning" },
  { key: "info", label: "info" },
];

function Divider() {
  return <span aria-hidden style={{ width: 1, height: 20, background: palette.hairline, flexShrink: 0 }} />;
}

function SeverityCountPill({
  severityKey,
  label,
  count,
}: {
  severityKey: string;
  label: string;
  count: number;
}) {
  const { t } = useI18n();
  const dotColor = count > 0 ? severityColor(severityKey) : severity.none;
  return (
    <Tooltip title={t("strip.severityFiring", { label, count })}>
      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
        <span
          aria-hidden
          style={{ width: 7, height: 7, borderRadius: "50%", background: dotColor, flexShrink: 0 }}
        />
        <Text style={{ fontSize: 12, color: palette.inkMuted }}>{label}</Text>
        <Text
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: palette.ink,
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {count}
        </Text>
      </span>
    </Tooltip>
  );
}

function HeartbeatDots() {
  const { clusters } = useClusterFilter();
  const { t } = useI18n();
  if (clusters.length === 0) return null;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
      {clusters.map((c) => (
        <Tooltip
          key={c.id}
          title={
            c.last_heartbeat_at
              ? t("strip.lastHeartbeat", {
                  cluster: c.display_name,
                  ago: dayjs(c.last_heartbeat_at).fromNow(),
                })
              : t("strip.noHeartbeat", { cluster: c.display_name })
          }
        >
          <span
            aria-hidden
            style={{
              width: 7,
              height: 7,
              borderRadius: "50%",
              background: heartbeatColor(c.heartbeat_state),
              display: "inline-block",
            }}
          />
        </Tooltip>
      ))}
    </span>
  );
}

function ConnectionDot() {
  const status = useAlertStreamStatus();
  const { t } = useI18n();
  return (
    <Tooltip title={t(CONNECTION_LABEL_KEY[status])}>
      <span
        aria-hidden
        style={{
          display: "inline-block",
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: status === "open" ? severity.ok : palette.inkMuted,
        }}
      />
    </Tooltip>
  );
}

interface StatusStripProps {
  /** Current section title, derived from the active route -- App.tsx owns
   * the route table already (it also drives the sider's selected key), so
   * it's passed down rather than duplicated here. */
  title: string;
}

/** The header strip: current section title, a live-status cluster (alert
 * severity counts + cluster heartbeats + SSE connection), and the
 * cluster/team/user controls. This is the one deliberately information-dense
 * element in an otherwise quiet shell -- everywhere else favors hairlines
 * over color and motion. */
export default function StatusStrip({ title }: StatusStripProps) {
  const { user, logout } = useAuth();
  const { currentTeam } = useTeam();
  const navigate = useNavigate();
  const { message } = AntApp.useApp();
  const { t, lang, setLang } = useI18n();
  const [webNotify, setWebNotify] = useState(isWebNotifyEnabled);
  const isAdmin = !!user?.is_admin;
  const teamId = currentTeam?.id;

  // A lightweight, header-scoped read of the live-alert feed for the count
  // pills. The queryKey starts with "alerts-live" so it rides the same SSE
  // invalidation useAlertStream already issues on every stream event
  // (TanStack matches invalidateQueries(["alerts-live"]) as a prefix), with
  // no header-specific SSE listener of its own.
  const liveQuery = useQuery({
    queryKey: ["alerts-live", "status-strip", teamId],
    queryFn: () => getLiveAlerts({ teamId }),
    enabled: isAdmin || !!teamId,
    refetchInterval: 30_000,
  });

  const counts = useMemo(() => {
    const tally: Record<string, number> = { critical: 0, warning: 0, info: 0 };
    for (const alert of liveQuery.data?.alerts ?? []) {
      if (alert.severity in tally) tally[alert.severity] += 1;
    }
    return tally;
  }, [liveQuery.data]);

  const handleToggleWebNotify = async (checked: boolean) => {
    const granted = await setWebNotifyEnabled(checked);
    setWebNotify(granted);
    if (checked && !granted) {
      message.warning(t("menu.webNotifyDenied"));
    }
  };

  const handleLogout = async () => {
    await logout();
    navigate("/login", { replace: true });
  };

  return (
    <div
      style={{
        height: 52,
        background: palette.surface,
        borderBottom: `1px solid ${palette.hairline}`,
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0 20px",
        gap: 20,
      }}
    >
      <Text style={{ fontSize: 16, fontWeight: 600, color: palette.ink, whiteSpace: "nowrap" }}>
        {title}
      </Text>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          flex: 1,
          justifyContent: "center",
          minWidth: 0,
          overflow: "hidden",
        }}
      >
        {(isAdmin || !!teamId) && (
          <>
            <Space size={16}>
              {LIVE_SEVERITIES.map((s) => (
                <SeverityCountPill key={s.key} severityKey={s.key} label={s.label} count={counts[s.key] ?? 0} />
              ))}
            </Space>
            <Divider />
          </>
        )}
        <HeartbeatDots />
        <ConnectionDot />
      </div>

      <div style={{ display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
        <ClusterFilterSelect />
        <TeamSwitcher />
        <Dropdown
          menu={{
            items: [
              {
                key: "web-notify",
                label: (
                  <div
                    style={{ display: "flex", justifyContent: "space-between", gap: 12 }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <span>{t("menu.webNotify")}</span>
                    <Switch size="small" checked={webNotify} onChange={handleToggleWebNotify} />
                  </div>
                ),
              },
              {
                key: "language",
                label: (
                  <div
                    style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <span>{t("menu.language")}</span>
                    <Segmented
                      size="small"
                      value={lang}
                      options={[
                        { label: "한국어", value: "ko" },
                        { label: "EN", value: "en" },
                      ]}
                      onChange={(value) => setLang(value as Lang)}
                    />
                  </div>
                ),
              },
              { key: "logout", label: t("menu.logout"), onClick: handleLogout },
            ],
          }}
        >
          <span style={{ cursor: "pointer", color: palette.ink, fontSize: 13 }}>
            {user?.display_name} ▾
          </span>
        </Dropdown>
      </div>
    </div>
  );
}
