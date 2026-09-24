import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import type { CurrentUser } from "./types";

export type AlertStreamEventType =
  | "alert_created"
  | "alert_resolved"
  | "alert_reopened"
  | "alert_acked"
  | "alert_unacked"
  | "comment_added";

export interface AlertStreamEvent {
  type: AlertStreamEventType;
  event_id: number;
  team_id: number | null;
  cluster: string;
  namespace: string | null;
  alertname: string;
  severity: string | null;
  is_test: boolean;
  ts: string;
}

const STREAM_URL = "/api/v1/events/stream";
const INVALIDATE_DEBOUNCE_MS = 2000;
const READY_STATE_POLL_MS = 5000;
export const WEB_NOTIFY_STORAGE_KEY = "kam.webNotify";

const STREAM_EVENT_TYPES: AlertStreamEventType[] = [
  "alert_created",
  "alert_resolved",
  "alert_reopened",
  "alert_acked",
  "alert_unacked",
  "comment_added",
];

// A single EventSource, module-scoped rather than per-component: owned by
// `useAlertStream` (called once, from AuthProvider), but also read by
// `useAlertStreamStatus` (called from the header's connection dot) --
// neither needs to be the other's parent/child for this to work.
let eventSource: EventSource | null = null;

export function isWebNotifyEnabled(): boolean {
  return localStorage.getItem(WEB_NOTIFY_STORAGE_KEY) === "1";
}

/**
 * Turn the browser-notification toggle on/off. Turning on requires (and, if
 * not yet decided, requests) `Notification` permission -- returns the
 * actually-resulting enabled state, since a user can flip the switch on but
 * have the browser deny or dismiss the permission prompt, in which case the
 * toggle must snap back to off rather than silently lying about its state.
 */
export async function setWebNotifyEnabled(enabled: boolean): Promise<boolean> {
  if (!enabled || !("Notification" in window)) {
    localStorage.setItem(WEB_NOTIFY_STORAGE_KEY, "0");
    return false;
  }
  let permission = Notification.permission;
  if (permission === "default") {
    permission = await Notification.requestPermission();
  }
  const granted = permission === "granted";
  localStorage.setItem(WEB_NOTIFY_STORAGE_KEY, granted ? "1" : "0");
  return granted;
}

/**
 * Owns the single EventSource for the live alert feed: connects once while
 * `user` is logged in, closes on logout (see AuthProvider, the only caller
 * -- this takes `user` as a parameter rather than calling `useAuth()`
 * itself because it runs inside AuthProvider's own render, before its
 * context value exists).
 *
 * On each event: debounced (2s) TanStack Query invalidation of the
 * affected list/detail queries -- ["alerts-live"]/["alert-history"] use
 * TanStack's key-prefix matching, so this invalidates every filter
 * variation of those queries, not just an exact key match -- plus (if
 * `kam.webNotify` is enabled and permission was granted) a browser
 * Notification for critical, non-test `alert_created` events.
 */
export function useAlertStream(user: CurrentUser | null): void {
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  useEffect(() => {
    if (!user) {
      eventSource?.close();
      eventSource = null;
      return;
    }

    const source = new EventSource(STREAM_URL, { withCredentials: true });
    eventSource = source;

    // Debounced by resetting a single timer on every incoming event: a
    // burst of alerts within the 2s window collapses into one flush of
    // every key that was touched during the burst, rather than one
    // refetch per event.
    const pendingKeys = new Map<string, unknown[]>();
    let flushTimer: number | null = null;

    const scheduleInvalidate = (queryKey: unknown[]) => {
      pendingKeys.set(JSON.stringify(queryKey), queryKey);
      if (flushTimer != null) window.clearTimeout(flushTimer);
      flushTimer = window.setTimeout(() => {
        flushTimer = null;
        const keys = Array.from(pendingKeys.values());
        pendingKeys.clear();
        for (const key of keys) {
          queryClient.invalidateQueries({ queryKey: key });
        }
      }, INVALIDATE_DEBOUNCE_MS);
    };

    const handleMessage = (evt: MessageEvent<string>) => {
      let parsed: AlertStreamEvent;
      try {
        parsed = JSON.parse(evt.data) as AlertStreamEvent;
      } catch {
        return; // malformed payload -- the next periodic refetch still covers it
      }

      scheduleInvalidate(["alerts-live"]);
      scheduleInvalidate(["alert-history"]);
      scheduleInvalidate(["ack-status"]);
      scheduleInvalidate(["alert-history-detail", parsed.event_id]);
      scheduleInvalidate(["alert-history-notifications", parsed.event_id]);
      scheduleInvalidate(["alert-comments", parsed.event_id]);

      if (
        parsed.type === "alert_created" &&
        parsed.severity === "critical" &&
        !parsed.is_test &&
        isWebNotifyEnabled() &&
        "Notification" in window &&
        Notification.permission === "granted"
      ) {
        const body = parsed.namespace ? `${parsed.cluster} / ${parsed.namespace}` : parsed.cluster;
        const notification = new Notification(`[KAM] ${parsed.alertname}`, {
          body,
          tag: `kam-alert-${parsed.event_id}`,
        });
        notification.onclick = () => {
          window.focus();
          navigate(`/alerts/history?highlight=${parsed.event_id}`);
          notification.close();
        };
      }
    };

    for (const type of STREAM_EVENT_TYPES) {
      source.addEventListener(type, handleMessage);
    }

    return () => {
      for (const type of STREAM_EVENT_TYPES) {
        source.removeEventListener(type, handleMessage);
      }
      source.close();
      if (eventSource === source) eventSource = null;
      if (flushTimer != null) window.clearTimeout(flushTimer);
      pendingKeys.clear();
    };
    // Reconnect only when the logged-in user actually changes (login/logout
    // or switching accounts), not on every AuthProvider render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);
}

export type AlertStreamStatus = "open" | "connecting" | "closed";

function readyStateLabel(readyState: number | undefined): AlertStreamStatus {
  if (readyState === EventSource.OPEN) return "open";
  if (readyState === EventSource.CONNECTING) return "connecting";
  return "closed";
}

/**
 * Polls the shared EventSource's `readyState` every 5s -- for the header's
 * connection indicator dot. A plain interval poll (rather than reacting to
 * the EventSource's own open/error events) is deliberate: `readyState`
 * during an auto-reconnect cycles through CONNECTING/OPEN/CLOSED faster
 * than is useful to reflect instantly, and the brief calls for a 5s poll.
 */
export function useAlertStreamStatus(): AlertStreamStatus {
  const [status, setStatus] = useState<AlertStreamStatus>(() =>
    readyStateLabel(eventSource?.readyState),
  );
  const statusRef = useRef(status);
  statusRef.current = status;

  useEffect(() => {
    const id = window.setInterval(() => {
      const next = readyStateLabel(eventSource?.readyState);
      if (next !== statusRef.current) setStatus(next);
    }, READY_STATE_POLL_MS);
    return () => window.clearInterval(id);
  }, []);

  return status;
}
