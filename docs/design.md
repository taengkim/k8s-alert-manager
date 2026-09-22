# K8s Alert Manager — 구현 계획

## Context

멀티팀 환경에서 Kubernetes 알럿을 통합 관리하는 웹 애플리케이션을 새로 만든다 (greenfield, 현재 저장소는 빈 상태). Prometheus/Alertmanager에서 알럿을 읽어오고, 알럿 룰을 관리하며, 팀별로 알림 채널을 설정해 앱이 직접 알림을 전송한다.

## 확정된 요구사항 (사용자 인터뷰 결과)

1. **기능 범위** (전부 포함):
   - 알럿 조회/대시보드 — Prometheus/Alertmanager에서 발생 중 알럿 읽기, 목록/필터/상세
   - 알럿 룰 CRUD — PrometheusRule CRD를 K8s API로 관리 (Prometheus Operator 전제)
   - **메트릭 기반 알럿 룰 생성**: Prometheus 메트릭을 UI에서 조회/탐색하고, 그 메트릭에 조건(PromQL 표현식 또는 임계값 빌더: 메트릭 선택 + 비교 연산자 + 임계값 + 지속시간)을 걸어 알럿 룰을 생성. 룰 저장 전 메트릭 쿼리 미리보기(차트) 제공
   - 사일런스/억제 관리 — Alertmanager API 사용
   - 알림 채널/라우팅 — 앱이 직접 전송
2. **스택**: Python (FastAPI) 백엔드 + React 프론트엔드
3. **알림 아키텍처**: Alertmanager webhook → FastAPI 수신 → 앱 내부 라우팅 → 채널 전송.
   - 단일 프로세스 + DB outbox 큐. asyncio 백그라운드 워커가 폴링, 지수 백오프 재시도. Redis/Celery 없음. 추후 워커 분리 가능한 구조.
4. **알림 채널**: 이메일(SMTP) 내장 + 사용자가 직접 개발해 끼워 넣는 플러그인 구조
5. **라우팅 regex 필터**: 채널별 라우팅 규칙에 regex 필터 지원 —
   - **포함(include) 필터**: 알럿(이름/라벨 등)이 regex에 매칭되는 것만 채널로 전송
   - **제외(exclude) 필터**: regex에 매칭되면 전송하지 않음 (차단)
   - 두 필터 조합 가능 (include 통과 후 exclude 적용)
   - **namespace / severity 레벨 조건**: 라우팅 규칙에서 namespace와 알럿 레벨(severity: critical/warning/info 등)로 알럿 전송 여부 제어 가능 (예: 특정 namespace의 critical만 전송, 특정 namespace는 전체 차단)
6. **인증/팀**:
   - LDAP 인증 필수
   - 팀 모델: 하이브리드 — LDAP 그룹 초기 매핑 + 앱 관리자가 팀/멤버 관리
   - 팀별 알럿 룰 등록, 팀별 채널 설정
   - 팀 간 알럿 공유 기능
7. **DB**: SQLAlchemy(async)로 SQLite(개발)/PostgreSQL(운영) 둘 다 지원, Alembic 마이그레이션
8. **개발 환경**: 로컬 올인원 — kind + kube-prometheus-stack(Helm) + Docker OpenLDAP, 스크립트로 자동 구축
9. **추가 선택 기능** (사용자 선택 확정):
   - 운영 워크플로우: Acknowledge/담당자 지정, 에스컬레이션(N분 내 미확인 시 다른 채널/팀 재전송), 런북 링크/노트(알럿 코멘트)
   - 노이즈 제어: 알럿 폭풍 제어(채널별 rate limit + digest 묶음 전송), 미해결 재알림(resolved까지 반복 알림)
   - 가시성: 알럿 통계 대시보드(Top N, 추이, 팀/namespace별 볼륨), 실시간 알럿 피드(WebSocket/SSE + 브라우저 알림), 감사 로그, 테스트 알럿 발사
10. **알럿 메시지 튜닝**: 알림 메시지 템플릿을 사용자가 커스터마이징 가능 — 제목/본문을 템플릿 변수(alertname, severity, namespace, labels, annotations 등)로 편집(Jinja2), 기본 템플릿 제공 + 채널/라우팅 규칙 단위 오버라이드, 저장 전 샘플 알럿으로 렌더링 미리보기
11. **멀티 클러스터 지원**: 여러 K8s 클러스터의 Prometheus/Alertmanager를 하나의 앱에서 통합 관리 —
    - 클러스터 등록/관리 (kubeconfig 또는 SA 토큰 + Prometheus/Alertmanager URL, 자격증명 암호화 저장)
    - 클러스터별 알럿 룰 CRUD (룰 생성 시 대상 클러스터 선택)
    - 알럿 수집 시 클러스터 귀속 (어느 클러스터에서 온 알럿인지 식별)
    - 대시보드/이력/통계에 클러스터 필터, 라우팅 규칙에도 클러스터 조건 추가
    - 사일런스도 대상 클러스터의 Alertmanager로
12. **수집 상태 감시 (deadman switch)**: 각 클러스터의 Watchdog(always-firing) 알럿 수신이 끊기면 "모니터링 파이프라인 장애"로 감지해 알림 — 멀티 클러스터 환경에서 클러스터별 heartbeat 상태 표시
13. **Grafana 딥링크**: 알럿에서 관련 Grafana 대시보드/패널로 이동하는 링크 (클러스터별 Grafana URL 설정)
14. **주간 리포트 자동 발송**: 팀별 알럿 통계 요약(볼륨, Top N, 추이)을 주기적으로 팀 채널로 전송
15. **데이터 보존 기간(retention) 자동 삭제**: 일정 기간이 지난 DB 데이터를 자동 삭제 — 알럿 이력(alert_events), 알림 전송 로그(notification_outbox), 감사 로그, 완료된 예약 작업 등 대상별 보존 기간 설정(관리자 설정 UI), 스케줄러가 주기적으로 배치 삭제
16. **JSON 익스포트/임포트**:
    - **알럿 룰 익스포트/임포트** — 룰 정의를 JSON으로 내보내기(단건/팀 단위 일괄)·가져오기(검증 + 충돌 처리, 다른 팀/클러스터로 이전 가능). 백업, 팀 간 공유, 클러스터 간 마이그레이션 용도
    - **알럿 이력 익스포트** — 필터링된 알럿 이력을 JSON으로 내보내기 (분석용)

## 기술 스택 (확정)

- **백엔드**: Python 3.12, FastAPI ≥0.115, SQLAlchemy 2.0 async (aiosqlite/asyncpg), Alembic, 공식 `kubernetes` 클라이언트(asyncio.to_thread 래핑), httpx, ldap3, PyJWT(httpOnly 쿠키), aiosmtplib, Jinja2, cryptography(Fernet — 채널 설정 암호화), pydantic-settings, uv
- **프론트엔드**: React 19 + TypeScript + Vite 7, TanStack Query v5, React Router 7, **Ant Design 5** (운영 대시보드에 최적 — 밀도 높은 Table/Form 내장)
- **테스트**: pytest + pytest-asyncio + respx / vitest

## 저장소 구조 (모노레포)

```
k8s-alert-manager/
├── Makefile, docker-compose.dev.yml   # OpenLDAP + Mailpit (+ Postgres 프로필)
├── backend/
│   ├── pyproject.toml, alembic/
│   ├── app/
│   │   ├── main.py, config.py, db.py, security.py
│   │   ├── models/, schemas/, api/     # 라우터: auth, teams, alerts, rules, silences, channels, routes, shares, webhook
│   │   ├── services/                   # ldap_auth, k8s, prometheus, alertmanager, rules, ingest, routing
│   │   ├── worker/outbox.py            # DB 큐 워커 (FastAPI 미의존 → 추후 분리 가능)
│   │   └── channels/                   # base.py(ABC), registry.py(플러그인 발견), email.py
│   └── tests/
├── frontend/src/                       # pages, api, auth, components
├── deploy/                             # Dockerfile, k8s 매니페스트 (SA + 최소 RBAC)
├── dev/                                # kind-config, setup.sh, kube-prometheus-values.yaml, ldap/bootstrap.ldif, seed/
└── plugins/example_webhook_channel/    # 외부 채널 플러그인 참조 구현
```

## 핵심 설계 결정

1. **PrometheusRule 관리**: 전용 네임스페이스 `kam-rules` + 라벨 소유권(`kam/team-id`, `app.kubernetes.io/managed-by: kam`). 룰 내 알럿에 `kam_team: <team.slug>` 라벨 주입 → 발생 알럿이 팀 귀속 정보를 갖고 webhook으로 돌아옴. `managed-by: kam` 없는 객체는 절대 건드리지 않음(kube-prometheus-stack 기본 룰 보호). Helm values에 `ruleSelectorNilUsesHelmValues: false` 필수.
2. **Webhook 중복 제거**: 알럿 identity = `(cluster_id, fingerprint, startsAt)` (그룹 페이로드는 identity 아님; cluster_id는 멀티 클러스터 섹션 참조 — AM fingerprint는 클러스터 간 충돌 가능). 신규 firing → insert + 라우팅, 반복 수신 → `receive_count`만 증가, firing→resolved 전환 시 라우팅. DB unique 제약이 동시성 백스톱.
3. **라우팅 엔진** (`services/routing.py`, 순수 함수):
   - 매처: `kind(include|exclude) + target(alertname|label|annotation) + key + regex pattern`
   - include는 AND(전부 매칭해야 통과, 0개면 전체 통과) → exclude는 OR(하나라도 매칭하면 차단, exclude가 항상 우선)
   - `re.search` 사용(앵커는 사용자가 `^$`로), 작성 시 `re.compile` 검증 + 512자 제한
   - 규칙 게이트: notify_on(firing/resolved), include_shared, enabled. 매칭되는 모든 규칙 발동, outbox는 `(event, channel, trigger)` 단위 dedup
   - **미리보기 API**: 초안 규칙을 최근 알럿 200건에 평가해 matched/blocked/not_included 반환
4. **Outbox 워커**: 3초 폴링, Postgres `FOR UPDATE SKIP LOCKED` claim(SQLite는 단순 branch), 지수 백오프(30s×2^n, 최대 30분, 8회 후 dead), lease 복구, 멀티 레플리카 대비 advisory lock.
5. **채널 플러그인**: `NotificationChannel` ABC (`type_name`, `config_schema: Pydantic 모델`, `async send()`). 발견: entry point `kam.channels` + `KAM_PLUGINS_DIR` 디렉터리 스캔. `config_schema.model_json_schema()`를 API로 노출 → 프론트가 JSON Schema 기반 동적 폼 렌더링(서드파티 플러그인도 프론트 작업 0). 설정은 Fernet 암호화 저장.
6. **LDAP 인증**: 서비스 계정 bind → 사용자 검색 → 사용자 DN re-bind(실제 인증) → 그룹 조회(memberOf 또는 그룹 검색 폴백) → 로컬 users upsert + `team_ldap_mappings` 동기화(`origin='ldap'` 행만 갱신, `manual` 행은 불변 — 하이브리드 모델). JWT는 httpOnly SameSite=Lax 쿠키.
7. **RBAC**: 전역 admin / 팀 owner(채널·라우팅·공유·멤버 관리) / 팀 member(알럿 조회, 룰 CRUD, 사일런스).
8. **팀 간 공유**: `alert_shares` — owner 팀이 target 팀에 부여, 매처로 범위 제한 가능. `view`(대시보드 표시만) / `view_notify`(target 팀의 `include_shared=true` 라우팅 규칙에도 평가) 두 모드.

## DB 스키마 (요약)

- **기본**: `users`, `teams`(slug 불변), `team_memberships`(role, origin), `team_ldap_mappings`
- **클러스터**: `clusters`(name slug, k8s_auth_kind, credentials_encrypted, prom/am/grafana_url, rules_namespace, webhook_token_hash, heartbeat 필드: enabled/alertname/timeout_seconds/team_id/last_heartbeat_at/state)
- **채널/라우팅**: `channels`(config_encrypted, template_id, rate_limit_per_hour, digest_mode/window, allow_cross_team_escalation), `routing_rules`(action notify|suppress, severities, namespaces_include/exclude, clusters, template_id, escalation_enabled/after_minutes, renotify_interval_minutes, notify_on_*, include_shared), `routing_matchers`(kind/target/key/pattern), `routing_rule_channels`, `routing_rule_escalation_channels`
- **알럿**: `alert_events`(UQ: cluster_id+fingerprint+starts_at, team_id nullable=미배정, severity/namespace/cluster_name denorm, suppressed_by_rule_id, acknowledged_at/by, assignee_user_id, is_test, receive_count), `alert_comments`
- **알림**: `notification_outbox`(status/attempts/next_attempt_at/locked_by, trigger: firing|resolved|escalation|renotify|digest|test|report, is_digest/digested_into_id, UQ: event+channel+trigger — 전송 로그 겸용), `scheduled_actions`(kind: escalation|renotify|digest_flush|test_resolve|…, due_at, status), `message_templates`(팀 스코프, title/body/body_html)
- **기타**: `alert_shares`, `silence_audit`(+cluster_id), `audit_log`, `report_schedules`(팀, 요일/시간, 채널, next_run_at), 설정(retention 기간 등 — app_settings)

## 로컬 개발 환경

- `docker-compose.dev.yml`: bitnami/openldap(alice/bob/carol, team-platform/team-payments/kam-admins 그룹) + Mailpit(SMTP 테스트)
- `dev/setup.sh`: kind(NodePort 30090/30093 매핑) → kube-prometheus-stack Helm(AM webhook → `host.docker.internal:8000`, repeat_interval 5m) → `kam-rules` ns + always-firing 시드 룰(`vector(1)`)
- 앱은 호스트에서 실행(`make backend` / `make frontend`), 클러스터가 host.docker.internal로 webhook 전달

## 구현 단계 (최종 — 전체 확정 범위)

| Phase | 내용 |
|---|---|
| 0 | 스캐폴드: 저장소 구조, FastAPI 골격, Alembic 베이스라인, Vite+antd 셸, Makefile, compose(LDAP+Mailpit), 이 설계 문서를 `docs/`에 커밋 |
| 1 | 개발 클러스터: kind + kube-prometheus-stack + 시드 룰 (`ruleSelectorNilUsesHelmValues: false`, NodePort, AM webhook → host) |
| 2 | 인증+팀+RBAC + **감사 로그 서비스** + **`clusters` 테이블·기본 클러스터 시드·클라이언트 팩토리 골격** (멀티클러스터 기반을 처음부터) |
| 3 | 실시간 알럿 대시보드 (Prom/AM 클라이언트 처음부터 클러스터 키 기반) |
| 4 | PrometheusRule CRUD (PromQL 모드) + runbook_url 필드; 룰 API에 cluster_id |
| 5 | 메트릭 브라우저 + 임계값 빌더 + 미리보기 차트; `/metrics/*` 프록시 (클러스터 키) |
| 6 | 사일런스 (클러스터 키) + silence_audit |
| 7 | Webhook 수신 + 이력: **토큰→클러스터 귀속 + `(cluster_id, fingerprint, starts_at)` identity를 첫 마이그레이션부터**; severity/namespace denorm |
| 8 | 채널 + 플러그인 프레임워크 + 이메일 내장 |
| 9 | 라우팅 엔진 (severity/namespace/suppress/regex + 클러스터 필터 0단계) + outbox 워커 |
| 10 | **테스트 알럿 발사 + Ack/담당자 + 코멘트** |
| — | **═══ MVP 컷라인 ═══** (단일 시드 클러스터로 메트릭 룰 작성, 알럿 조회/사일런스/Ack, 필터링된 이메일 알림, 전체 파이프라인 테스트 가능) |
| 11 | 멀티 클러스터 GA + **Grafana 딥링크**: `/admin/clusters` UI(grafana_url 포함), 헬스체크, 토큰 회전, 헤더 클러스터 필터, 클러스터 칩, 쓰기 폼 클러스터 선택, fan-out, 개발용 시뮬레이션/2번째 클러스터 스크립트 |
| 12 | JSON 익스포트/임포트: 룰 익스포트/임포트(dry-run + 충돌 처리 + 리타게팅), 이력 익스포트(json/ndjson) |
| 13 | 메시지 템플릿: 라이브러리, 샌드박스 렌더링, 우선순위, 미리보기, 편집기, ABC `send(n, msg)` 변경 |
| 14 | 팀 간 공유 (view / view_notify + include_shared) |
| 15 | 스케줄러 코어 + 에스컬레이션 + 미해결 재알림 + **데이터 retention 자동 삭제** |
| 16 | 폭풍 제어: rate limit + digest + `send_batch` |
| 17 | **수집 상태 감시(deadman)**: 60s 스윕 + 상태 머신 + 합성 알럿/복구를 실제 ingest로 + UI 표시/배너 |
| 18 | 실시간 피드: SSE + 브라우저 알림 |
| 19 | 통계 대시보드 + 감사 로그 뷰어 UI (공용 `services/stats.py` 추출) |
| 20 | **주간 리포트**: report_schedules + 스케줄러 트리거 + 리포트 템플릿(kind='report') + `send_message` + Reports 탭 |
| 21 | 패키징/배포: Docker 멀티스테이지, k8s 매니페스트, SA 최소 RBAC(클러스터별 최소권한), Postgres, 인클러스터 E2E 재실행 |

post-MVP 의존성: 16→15, 17→15(스케줄러 루프), 20→{13 템플릿, 15 스케줄러, 19 stats}; 그 외 상호 독립. 실배포가 급하면 21을 앞당길 수 있음. Phase 1의 dev values에 **Watchdog을 kam receiver로 라우팅**(기본은 null receiver) 포함, Phase 7에 heartbeat ingest 훅(타임스탬프만) 포함.

핵심 검증 포인트: P10 테스트 알럿이 채널별 전송 상태와 함께 전체 파이프라인 추적 / P11 같은 알럿을 두 토큰으로 수신 → 클러스터별 2개 이벤트(복합 identity 증명) / P13 샌드박스가 `{{ ''.__class__ }}` 차단, 렌더 실패 시 기본 템플릿 폴백 / P15 미ack 알럿 N분 후 에스컬레이션, ack 시 취소 / P16 연속 테스트 알럿 20건 → digest 이메일 1건 / P17 두 번째 브라우저 탭이 새로고침 없이 갱신

## 주요 리스크

- AM은 자체 인가 없음 → 앱은 편의 게이트웨이지 보안 경계 아님
- 사용자 regex의 catastrophic backtracking → 길이 제한+컴파일 검증, 필요 시 google-re2
- 앱 다운 시 webhook 유실 → post-MVP: AM `/api/v2/alerts` 대사(reconcile) 잡
- LDAP 그룹 동기화는 로그인 시점만 → 12h 토큰 TTL + is_active 킬스위치

## 검증 방법

각 Phase마다: pytest 단위/통합 테스트(respx로 AM/Prom 모킹, 임시 디렉터리 플러그인 발견 테스트, 테이블 주도 매처 엔진 테스트) + kind 클러스터 E2E(UI에서 룰 생성 → kubectl로 CRD 확인 → Prometheus rules API → 알럿 발생 → Mailpit에서 이메일 수신 확인, exclude 매처 추가 → 미전송 확인, Mailpit 중단 → 백오프 → 재개 → 전송 확인).

## 라우팅 확장: namespace/severity 1급 필터 + suppress 액션

- `routing_rules`에 전용 컬럼 추가: `action('notify'|'suppress')`, `severities`(json, null=전체, `none`=severity 라벨 없음 매칭), `namespaces_include`/`namespaces_exclude`(json — 각 항목을 anchored regex `re.fullmatch`로 평가; 네임스페이스 이름은 DNS-1123이라 정확일치·regex가 한 메커니즘으로 통합됨)
- **suppress 규칙이 모든 notify 규칙보다 먼저 평가** — 매칭 시 해당 팀에 알림 없음, 이력에는 `suppressed_by_rule_id` 기록. "namespace Y 전체 차단" = suppress 규칙 하나
- 평가 순서: 게이트(enabled/notify_on/include_shared) → severity → ns include → ns exclude → 일반 include regex(AND) → 일반 exclude regex(OR, 항상 우선)
- namespace 라벨 없는 알럿(Watchdog 등): include 비어있을 때만 통과, exclude는 차단 안 함
- suppress 규칙은 `channel_ids` 비어 있어야 함(서버 검증)

## 메트릭 기반 룰 편집기 (확정 설계)

- **차트: ECharts**(echarts-for-react) — 밀집 시계열, threshold markLine, dataZoom, 통계 대시보드와 공용
- 백엔드 `/api/v1/metrics/*` 프록시 (인증 + 가드레일: 범위≤7d, step 자동 보정 ≤500pt/series, ≤50 series, 15s 타임아웃): `names`(60s 캐시+검색), `metadata`, `labels`, `label-values`, `query`(즉시 — "지금 발생?" 표시), `query_range`(미리보기 차트)
- 편집기 모드: **임계값 빌더 | PromQL** 토글. 빌더 = 메트릭 자동완성(이름+타입+help) → 라벨 필터 행 → 비교 연산자 → 임계값 → for 지속시간 → 생성된 PromQL 읽기전용 표시. 서버는 생성 PromQL 재검증
- **빌더 상태 왕복**: 빌더 JSON을 CRD annotation `kam.io/builder-v1`에 저장. 재열기 시 빌더 모드 복원; 밖에서 expr 수정되면 PromQL 모드로 폴백

## 운영 기능 설계 (B1–B9)

**공통 기반 — 워커에 경량 스케줄러 추가** (신규 인프라 없음): `scheduled_actions` 테이블(kind: escalation|renotify|digest_flush|test_resolve, due_at, status) — 워커 루프가 outbox와 같은 SKIP LOCKED 패턴으로 클레임. `notification_outbox.trigger` 확장: firing|resolved|escalation|renotify|digest|test

1. **Ack/담당자**: `alert_events`에 acknowledged_at/by, assignee_user_id. API: ack/unack/assignee. Ack 시 해당 이벤트의 escalation·renotify 예약 취소
2. **에스컬레이션**: 라우팅 규칙에 부착(escalation_enabled, after_minutes + 에스컬레이션 채널 조인 테이블). 크로스팀 대상은 `channels.allow_cross_team_escalation` 옵트인. ingest 시 예약 → 만기 시 여전히 firing+미ack면 전송
3. **런북/노트**: 런북 URL = PrometheusRule 표준 `runbook_url` annotation(DB 불필요, 알림에 자동 포함). 노트 = `alert_comments` 테이블 + 상세 페이지 코멘트 스레드
4. **폭풍 제어**: `channels`에 rate_limit_per_hour, digest_mode(off|auto|always), digest_window_minutes. 초과 시 outbox 행을 'digested' 상태로 파킹 → digest_flush 예약 → 하나의 묶음 알림으로 전송. 플러그인 ABC에 `send_batch()` 추가(기본 구현: 루프, 이메일은 요약 메일 오버라이드 — 기존 플러그인 하위호환)
5. **미해결 재알림**: `routing_rules.renotify_interval_minutes`. firing 전송 성공 후 예약 → 만기 시 여전히 firing+미ack면 재전송+재예약. resolve/ack로 중단. 폭풍 제어 적용받음
6. **통계 대시보드**: `/stats/*` API (top-alerts, volume, breakdown by ns/severity/team, MTTA/MTTR) — alert_events 집계(denorm 컬럼 덕에 저렴, is_test 제외). `/stats` 페이지 ECharts
7. **실시간 피드: SSE** (sse-starlette) — 서버→클라이언트 단방향이라 WS 불필요, EventSource 자동 재연결+쿠키 인증+프록시 무설정. in-process 허브(멀티 레플리카 시 PG LISTEN/NOTIFY로 교체). `useAlertStream()` 훅이 TanStack Query 캐시 무효화 + 브라우저 Notification API
8. **감사 로그**: `audit_log` 테이블 + `services/audit.py` — Phase 2부터 모든 변경 엔드포인트에 삽입(추후 소급 아님). 팀 owner는 팀 범위, admin은 전체 조회
9. **테스트 알럿 발사**: `POST /teams/{id}/test-alert` → AM webhook v4 형태 합성(fingerprint `test-` 접두사, `is_test=true`) → 실제 ingest 파이프라인 통과 → 매칭/차단 판정 + 채널별 전송 상태 실시간 추적 UI. 5분 후 자동 resolve 예약. 통계에서 제외

## 알림 메시지 템플릿 (확정 설계)

- **엔진**: Jinja2 `SandboxedEnvironment` + `ChainableUndefined`(누락 변수는 운영 전송 시 빈 값 — 알림 전송이 템플릿 오류로 죽지 않게). 가드레일: 16KB 제한, to_thread + 2s 타임아웃 렌더, 출력 256KB 캡, HTML 슬롯만 autoescape
- **슬롯**: title / body / body_html(선택) — 채널 타입별로 사용하는 슬롯 선언
- **저장**: `message_templates` 테이블 — 팀 스코프 이름 있는 템플릿 라이브러리(재사용). `channels.template_id`, `routing_rules.template_id` (NULL=상속)
- **우선순위**: 라우팅 규칙 템플릿 → 채널 템플릿 → 채널 타입 기본값(`NotificationChannel.default_templates`) → 앱 기본값. **렌더링은 워커의 전송 시점**(outbox payload는 구조화 상태 유지 — 템플릿 수정이 대기 중 항목에도 적용, digest 렌더 가능)
- **렌더 실패 시**: 채널 타입 기본 템플릿으로 폴백 + outbox 행에 경고 기록 — 잘못된 템플릿이 알림을 죽이지 않음
- **ABC 변경**: `send(n: AlertNotification, msg: RenderedMessage)` / `send_batch(...)` — 플러그인은 msg 사용 또는 자체 포맷 중 선택
- **API**: 템플릿 CRUD + `POST /templates/preview`(문법 오류 위치 + 미정의 변수 경고 반환) + `GET /templates/variables`(AlertNotification 모델에서 자동 생성되는 변수 레퍼런스)
- **UI**: `/templates` 페이지 — 슬롯 편집기 + 변수 클릭 삽입 사이드바 + 실시간 미리보기(샘플 알럿 또는 최근 이벤트 선택)

## 멀티 클러스터 (확정 설계)

**핵심 결정**:
- **클러스터 귀속 = 클러스터별 webhook 토큰이 1차 identity** (인증되므로 위조 불가; `kam_cluster` external label은 보조/검증용)
- **dedup identity = `(cluster_id, fingerprint, starts_at)`** — AM fingerprint는 라벨셋 해시라 클러스터 간 충돌이 실제로 발생. 첫 마이그레이션부터 복합 unique 제약
- **레지스트리 추상화는 초기(Phase 2)부터, 관리 UI는 post-MVP** — cluster_id를 나중에 소급하면 전 서브시스템 수정이라 처음부터 클러스터 키 기반으로. MVP는 시드된 기본 클러스터 1개로 동작
- **UI 스코핑**: 읽기 뷰는 헤더의 클러스터 멀티셀렉트 필터(기본 All), 쓰기 작업(룰 생성/사일런스)은 폼에서 클러스터 명시 선택
- **네트워크**: hub-and-spoke — 앱이 모든 클러스터 API/Prom/AM에 도달해야 함. 폐쇄망용 pull agent는 post-MVP 리스크로 기록

**스키마**: `clusters` 테이블(name slug, k8s_auth_kind: incluster|kubeconfig|token, credentials_encrypted(Fernet), prometheus/alertmanager_url, rules_namespace, webhook_token_hash — 평문은 생성 시 1회만 표시). `alert_events.cluster_id` + denorm cluster_name, `routing_rules.clusters`(json, null=전체), `silence_audit.cluster_id`. 시작 시 기본 클러스터 행 자동 시드(env 기반, 멱등)

**백엔드**: K8s/Prom/AM 클라이언트 팩토리 — `(cluster_id, updated_at)` 키 LRU 캐시(자격증명 교체 시 자동 무효화). 라이브 알럿은 클러스터 병렬 fan-out(클러스터당 5s 타임아웃, 부분 실패 허용 — 죽은 클러스터가 대시보드를 막지 않음). 라우팅 평가 0단계에 클러스터 필터 추가(가장 저렴). `/metrics/*`, `/rules/*`, `/silences`, `/namespaces` 모두 cluster_id 파라미터. 헬스체크 서비스(K8s/Prom/AM 각각, 30s 캐시)

**UI**: `/admin/clusters` — 컴포넌트별 헬스 dot, 추가/편집 drawer, webhook 토큰 회전(1회 표시 + AM 설정 YAML 스니펫 제공), 활성/비활성. 전 테이블에 클러스터 칩/컬럼

**개발 환경**: 기본은 같은 kind 클러스터를 이름 2개로 등록(저비용 시뮬레이션 — 토큰 귀속/복합 dedup/필터 검증), 선택적으로 `make dev-up-2`로 실제 2번째 kind 클러스터

**신규 리스크**: 크로스 클러스터 자격증명 보관(Fernet + admin 전용 + 쓰기 후 미반환, 클러스터별 최소권한 SA 권장), hub 도달성(부분 성능저하 설계), 클러스터 간 fingerprint 충돌(복합 키로 구조적 해결)

## JSON 익스포트/임포트 (확정 설계)

- **버저닝된 envelope**: `{"kam_export_version": 1, "kind": "rules", source: {...}, rules: [...]}`. 익스포트 시 소유권 메타데이터(kam_team 라벨, kam/team-id, managed-by, 이름 접두사) 전부 제거 → 이식 가능한 순수 룰 정의. `kam.io/builder-v1` annotation과 runbook_url은 보존(빌더 모드 왕복 유지). 미지원 버전은 400
- **임포트 = 검증 → 룰별 verdict → 쓰기**: 같은 엔드포인트의 `dry_run=true`가 미리보기 모달 구동. Pydantic → slug → PromQL 검증(**대상 클러스터** Prometheus로). 대상에 없는 메트릭 참조는 경고(오류 아님). 한 룰 실패가 배치를 중단하지 않음
- **충돌 처리**: `conflict_strategy: skip|overwrite|rename`(rename은 `-2`,`-3` 접미사). verdict: created|skipped|overwritten|renamed|failed
- **리타게팅**: 대상 팀 = URL의 팀(소유 팀만, admin은 전체), 대상 클러스터 = `target_cluster_id`. 쓰기 시 대상 기준으로 소유권 라벨 재주입
- **범위**: MVP는 룰만. 라우팅 규칙 익스포트는 채널 참조 문제로 연기, 채널 설정 익스포트는 비권장(시크릿 제외하면 의미 없음)
- **이력 익스포트**: 익스포트 전용, 이력 조회 필터 그대로 적용. `format=json`(1만 행 캡) | `ndjson`(스트리밍, 10만 행 캡)
- 익스포트/임포트 모두 audit_log 기록
- **UI**: Rules 페이지 툴바 Export/Import(임포트 모달: 업로드 → 대상 클러스터 → 전략 → dry-run 미리보기 테이블 → 확정), History 페이지 Export 버튼(필터 반영 + 행 수 예상치)
- 신규 모듈: `services/rule_transfer.py` (envelope 빌드/파싱, 버전 게이트, 소유권 제거/재주입, verdict 파이프라인)

## 소형 기능 설계 (수집 상태 감시 · Grafana · 주간 리포트 · Retention)

**수집 상태 감시 (deadman switch)** — 에지 트리거 상태 머신:
- `clusters`에 heartbeat 필드: `heartbeat_enabled`, `heartbeat_alertname`(기본 'Watchdog'), `heartbeat_timeout_seconds`(기본 600 ≈ repeat_interval 2배), `heartbeat_team_id`(합성 알럿 귀속 팀, NULL=미배정/admin), `last_heartbeat_at`, `heartbeat_state('unknown'|'ok'|'missing')`. 별도 테이블 없음 — 장애 이력은 합성 alert_events가 담당
- ingest 훅: alertname이 heartbeat_alertname과 일치하면 `last_heartbeat_at`만 갱신하고 이벤트 생성/라우팅에서 **제외**
- 스케줄러 60초 고정 스윕: timeout 초과 & state≠missing → missing 전환 + 합성 firing 이벤트(`KamClusterHeartbeatLost`, 결정적 fingerprint `hb-<cluster_id>`, severity critical)를 실제 ingest로 주입. heartbeat 재개 & state=missing → ok 전환 + 해당 이벤트 resolve(복구 알림은 각 규칙의 notify_on_resolved 따름). **상태 전환 시에만** 발생 → 장애당 정확히 1회 알림 + 1회 복구
- UI: `/admin/clusters`에 상태 dot + "마지막 수신 X분 전", 클러스터 missing 시 Alerts 페이지 전역 경고 배너
- **개발 환경 주의**: kube-prometheus-stack 기본 AM 설정은 Watchdog을 null receiver로 보냄 — values에서 kam webhook receiver로도 라우팅 필수

**Grafana 딥링크** — `services/grafana.py` 우선순위: ① 룰 annotation `kam.io/grafana-url`(룰 편집기 필드) → ② `clusters.grafana_url` 설정 시 알럿 라벨 기반 Explore URL 구성 → ③ 없음. `AlertNotification.grafana_url` 변수로 템플릿/이메일에 자동 노출. generator_url(Prometheus)은 항상 별도 표시

**주간 리포트** — 리포트는 "또 하나의 알림 타입":
- `report_schedules`(team_id, cadence weekly|daily|monthly, weekday, hour, timezone, template_id NULL=내장 리포트 템플릿, next_run_at, last_run_at, last_status) + `report_schedule_channels` 조인
- `message_templates.kind('alert'|'report')` 추가 — kind별 변수/검증 분리. `notification_outbox.alert_event_id` NULLABLE로 변경(리포트 행은 이벤트 없음) + trigger 'report'
- 스케줄러 스윕이 due 스케줄 클레임 → 공용 `services/stats.py` 집계(볼륨, Top-N, 직전 동기간 대비 증감, 클러스터/namespace별, MTTA/MTTR) → 샌드박스 렌더 → 채널별 outbox 행
- 플러그인 ABC에 `send_message(msg)` 추가(이벤트 없는 콘텐츠용; 기본 구현은 send() 어댑터 — 기존 플러그인 호환, 이메일은 리포트 레이아웃 오버라이드)
- API: 스케줄 CRUD + `POST /reports/{id}/run-now`(수동 테스트) + `GET /reports/{id}/preview`(발송 없이 현재 기간 렌더). UI: 팀 설정 "Reports" 탭

**데이터 retention 자동 삭제**: 대상별 보존 기간(app_settings 저장, 관리자 UI에서 수정) — alert_events는 resolved만 기본 90일(진행 중 firing은 절대 삭제 안 함), outbox 종결 행 30일, audit_log 365일, scheduled_actions done/cancelled 7일, alert_comments는 이벤트와 FK cascade로 함께 삭제. 스케줄러의 일일 purge 잡이 1000행 단위 배치 DELETE(긴 락 방지), 삭제 요약을 audit_log에 기록
