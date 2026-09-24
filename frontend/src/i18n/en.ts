import type { ko } from "./ko";

/** English dictionary. Typed against ko's key set -- adding a key to ko.ts
 * without an English entry here fails the build. */
export const en: Record<keyof typeof ko, string> = {
  "app.title": "K8s Alert Manager",

  "nav.alerts": "Alerts",
  "nav.history": "Alert history",
  "nav.rules": "Rules",
  "nav.silences": "Silences",
  "nav.channels": "Channels",
  "nav.templates": "Templates",
  "nav.routes": "Routing",
  "nav.shares": "Shares",
  "nav.stats": "Statistics",
  "nav.team": "Team settings",
  "nav.admin": "Admin",

  "shell.clustersLoadError":
    "Couldn't load the cluster list — what's shown may be incomplete",

  "strip.connOpen": "Live connection open",
  "strip.connConnecting": "Reconnecting...",
  "strip.connClosed": "Disconnected",
  "strip.severityFiring": "{count} {label} firing",
  "strip.lastHeartbeat": "{cluster} · last received {ago}",
  "strip.noHeartbeat": "{cluster} · no heartbeats yet",

  "menu.webNotify": "Browser notifications",
  "menu.webNotifyDenied": "Browser notification permission was denied",
  "menu.logout": "Log out",
  "menu.language": "Language",
};
