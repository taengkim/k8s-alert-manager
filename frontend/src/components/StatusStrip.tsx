import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import dayjs from "dayjs";
import relativeTime from "dayjs/plugin/relativeTime";
import { App as AntApp, Dropdown, Space, Switch, Tooltip, Typography } from "antd";
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
import TeamSwitcher from "./TeamSwitcher";
import ClusterFilterSelect from "./ClusterFilterSelect";

dayjs.extend(relativeTime);

const { Text } = Typography;

const CONNECTION_LABEL: Record<AlertStreamStatus, string> = {
  open: "실시간 연결됨",
  connecting: "재연결 중...",
  closed: "연결 끊김",
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
  const dotColor = count > 0 ? severityColor(severityKey) : severity.none;
  return (
    <Tooltip title={`${label} ${count}건 발생 중`}>
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
  if (clusters.length === 0) return null;
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
      {clusters.map((c) => (
        <Tooltip
          key={c.id}
          title={`${c.display_name} · ${
            c.last_heartbeat_at ? `마지막 수신 ${dayjs(c.last_heartbeat_at).fromNow()}` : "수신 이력 없음"
          }`}
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
  return (
    <Tooltip title={CONNECTION_LABEL[status]}>
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
      message.warning("브라우저 알림 권한이 거부되었습니다");
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
                    <span>브라우저 알림</span>
                    <Switch size="small" checked={webNotify} onChange={handleToggleWebNotify} />
                  </div>
                ),
              },
              { key: "logout", label: "로그아웃", onClick: handleLogout },
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
