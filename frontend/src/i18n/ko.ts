/**
 * Korean dictionary -- the canonical key set. Every UI string in the app
 * goes through a key defined here; en.ts is typed against this object, so
 * a key missing from either dictionary is a compile error, not a silent
 * fallback. Keys are namespaced by area: nav.*, strip.*, common.*, then
 * one namespace per page.
 */
export const ko = {
  "app.title": "K8s Alert Manager",

  "nav.alerts": "알럿",
  "nav.history": "알럿 이력",
  "nav.rules": "룰",
  "nav.silences": "사일런스",
  "nav.channels": "채널",
  "nav.templates": "템플릿",
  "nav.routes": "라우팅",
  "nav.shares": "공유",
  "nav.stats": "통계",
  "nav.team": "팀 설정",
  "nav.admin": "관리자",

  "shell.clustersLoadError":
    "클러스터 목록을 불러오지 못했습니다 — 표시된 목록이 불완전할 수 있습니다",

  "strip.connOpen": "실시간 연결됨",
  "strip.connConnecting": "재연결 중...",
  "strip.connClosed": "연결 끊김",
  "strip.severityFiring": "{label} {count}건 발생 중",
  "strip.lastHeartbeat": "{cluster} · 마지막 수신 {ago}",
  "strip.noHeartbeat": "{cluster} · 수신 이력 없음",

  "menu.webNotify": "브라우저 알림",
  "menu.webNotifyDenied": "브라우저 알림 권한이 거부되었습니다",
  "menu.logout": "로그아웃",
  "menu.language": "언어",
} as const;
