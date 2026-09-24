# k8s-alert-manager (KAM)

멀티팀 · 멀티클러스터 환경을 위한 Kubernetes 알럿 관리 웹 애플리케이션입니다.
Prometheus/Alertmanager에서 발생 중인 알럿을 읽고, PrometheusRule을 UI에서 관리하며,
Alertmanager webhook으로 수신한 알럿을 팀별 라우팅 규칙에 따라 앱이 직접 알림 채널로
전송합니다.

전체 설계 문서: [`docs/design.md`](./docs/design.md) ·
배포 가이드: [`deploy/README.md`](./deploy/README.md)

## 주요 기능

**알럿 조회/운영**

- 실시간 알럿 대시보드(클러스터 병렬 fan-out, 부분 실패 허용) + 알럿 이력(수신/해소 기록)
- Acknowledge/담당자 지정, 알럿 코멘트(노트), 런북 링크(`runbook_url` annotation)
- SSE 실시간 피드(새 알럿 시 목록 자동 갱신 + 브라우저 알림 옵션)
- 테스트 알럿 발사: 합성 알럿을 실제 파이프라인에 통과시켜 매칭/차단/채널 전송 상태 추적
- 헤더 StatusStrip: 어느 화면에서든 발생 중 알럿 수(심각도별) · 클러스터 heartbeat · SSE 연결 상태 표시

**알럿 룰 (PrometheusRule CRD)**

- 팀별 룰 CRUD — Prometheus Operator의 PrometheusRule을 K8s API로 직접 관리
- 임계값 빌더: 메트릭 자동완성 → 라벨 필터 → 비교 연산자/임계값/지속시간 → PromQL 자동 생성,
  저장 전 미리보기 차트(ECharts, threshold 라인 포함)
- PromQL 직접 입력 모드(서버 측 검증), 빌더 상태는 CRD annotation으로 왕복 보존
- JSON 익스포트/임포트: dry-run 미리보기, 충돌 전략(skip/overwrite/rename), 팀·클러스터 리타게팅

**알림 라우팅/전송**

- Alertmanager webhook 수신 → 클러스터별 토큰 인증 → `(cluster_id, fingerprint, starts_at)` 복합
  identity로 중복 제거 → 팀 라우팅 평가
- 라우팅 규칙: notify/suppress 액션, severity·namespace(정규식) 조건, 클러스터 필터,
  include(AND)/exclude(OR, 항상 우선) 정규식 매처, 초안 규칙을 최근 알럿에 미리 평가하는 프리뷰
- DB outbox 큐 + 백그라운드 워커(지수 백오프 재시도, dead 처리) — Redis/Celery 불필요
- 폭풍 제어: 채널별 rate limit + digest 묶음 전송 / 에스컬레이션(N분 내 미확인 시 다른 채널) /
  미해결 재알림
- 채널: 이메일(SMTP) 내장 + 파이썬 플러그인으로 채널 타입 추가 가능(아래 참고)
- 메시지 템플릿: Jinja2(샌드박스) 제목/본문 커스터마이징, 채널/규칙 단위 오버라이드,
  렌더 실패 시 기본 템플릿 폴백

**팀/인증**

- LDAP 인증(그룹 → 팀 자동 매핑) + 앱 관리자의 수동 팀 관리(하이브리드)
- RBAC: 전역 admin / 팀 owner / 팀 member
- 팀 간 알럿 공유: view / view_notify 모드, 매처로 공유 범위 제한(컴파일 실패 시 fail-closed)

**멀티 클러스터**

- 클러스터 등록(kubeconfig/토큰/in-cluster, 자격증명 Fernet 암호화), 컴포넌트별 헬스체크
- 클러스터별 webhook 토큰이 알럿 귀속의 1차 identity, 토큰 회전 지원
- 수집 상태 감시(deadman): Watchdog 알럿 수신이 끊기면 합성 critical 알럿으로 파이프라인 장애 감지
- Grafana 딥링크(룰 annotation 또는 클러스터 Grafana URL 기반)

**가시성/관리**

- 통계 대시보드: Top-N, 발생 추이, 심각도/네임스페이스 분포, MTTA/MTTR
- 주간(일간/월간) 리포트 자동 발송 + 미리보기/즉시 발송
- 감사 로그(모든 변경 기록), 데이터 보존 기간별 자동 삭제(retention)
- 알럿 이력 JSON/NDJSON 익스포트
- UI 한국어/영어 전환(사용자 메뉴 → 언어, 선택 유지)

## 아키텍처

```
 ┌─ Cluster A ─────────────┐        ┌──────────────────── KAM (단일 프로세스) ───────────────────┐
 │ Prometheus  Alertmanager│ webhook│  FastAPI ──── ingest(중복제거·팀귀속) ── 라우팅 엔진        │
 │     ▲            │      │───────▶│    │                                       │              │
 │ PrometheusRule ◀─┼──────┼────────│  K8s/Prom/AM 클라이언트 팩토리          notification_outbox │
 └──────────────────┼──────┘  CRUD  │  (클러스터 키 기반 LRU)                     │              │
 ┌─ Cluster B ──────┼──────┐        │                                    워커(폴링·백오프·스케줄러)│
 │      ...         │      │        │  SQLite(dev) / PostgreSQL(prod)             │              │
 └──────────────────┴──────┘        └──────────────────────────────────────────┬──┘              │
                                                 React SPA (antd) ◀── SSE ──┘  └─▶ Email/플러그인 채널
```

핵심 설계 결정:

- **단일 프로세스 + DB outbox 큐** — 브로커 없이 전송 보장(재시도/백오프/전송 로그 겸용).
  워커는 FastAPI에 의존하지 않아 추후 별도 프로세스로 분리 가능(`KAM_WORKER_MODE`).
- **클러스터별 webhook 토큰 = 알럿 귀속 identity** — 위조 불가, fingerprint 충돌은
  `(cluster_id, fingerprint, starts_at)` 복합 unique로 구조적 해결.
- **PrometheusRule 소유권 라벨** — `app.kubernetes.io/managed-by: kam` 없는 객체는 절대 건드리지
  않음(kube-prometheus-stack 기본 룰 보호). 룰에 `kam_team` 라벨을 주입해 발생 알럿이 팀 정보를
  갖고 돌아옴.
- **suppress 규칙이 notify보다 항상 먼저** — "이 네임스페이스 전체 차단"이 규칙 하나로 표현됨.

## 기술 스택

| 영역 | 스택 |
|---|---|
| 백엔드 | Python 3.12, FastAPI, SQLAlchemy 2.0 async, Alembic, kubernetes client, httpx, ldap3, Jinja2(sandbox), cryptography(Fernet), sse-starlette, uv |
| 프론트엔드 | React 19, TypeScript, Vite 7, Ant Design 5, TanStack Query v5, React Router 7, ECharts |
| DB | SQLite(개발) / PostgreSQL(운영) — 동일 코드, Alembic 마이그레이션 공용 |
| 테스트 | pytest(+asyncio, respx) 880+ 케이스, PostgreSQL 프로필 재실행 지원 |

## 저장소 구조

```
backend/
  app/
    api/          # 라우터: auth, teams, alerts, rules, silences, channels, routes,
                  #   shares, templates, stats, reports, clusters, webhook, events(SSE), ...
    services/     # ingest, routing, sharing, templating, stats, reports, retention,
                  #   k8s, grafana, cluster_health, rule_transfer, ...
    worker/       # outbox 워커, 경량 스케줄러(에스컬레이션/재알림/digest/retention/리포트/heartbeat)
    channels/     # base.py(플러그인 ABC), email.py(내장), registry.py(발견)
    models/ schemas/ alembic/
  tests/
frontend/
  src/pages/      # Alerts, AlertHistory, Rules, RuleEditor, Silences, Channels, Routes,
                  #   Shares, Templates, Stats, TeamSettings, Admin, AdminClusters, ...
  src/components/ # StatusStrip, TestAlertModal, SilenceModal, JsonSchemaForm, rule-editor/, ...
  src/i18n/       # ko/en 사전 + I18nProvider (en은 타입으로 완전 커버리지 강제)
  src/theme.ts    # 디자인 토큰 단일 정본 (팔레트/심각도 색/차트 팔레트/antd 테마)
deploy/           # Dockerfile(멀티스테이지), k8s 매니페스트(최소 RBAC), 배포 README
dev/              # kind 설정, kube-prometheus-stack values, LDAP 부트스트랩, setup/teardown
plugins/          # 채널 플러그인 참조 구현 (example_webhook_channel)
docs/design.md    # 승인된 전체 설계 문서
```

## 시작하기 (로컬 올인원 개발 환경)

### 요구사항

`docker`(compose 포함), `kind`, `helm`, `kubectl`, `uv`, `node`(>= 20)

### 1) 의존 서비스 + 개발 클러스터

```bash
make dev-deps   # OpenLDAP(:1389) + Mailpit(SMTP :1025 / UI :8025) 기동
make dev-up     # 위 + kind 클러스터 'kam' 생성 + kube-prometheus-stack 설치
                #   (Prometheus NodePort :30090, Alertmanager :30093,
                #    AM webhook -> host.docker.internal:8000, kam-rules 네임스페이스 시드)
```

PostgreSQL로 개발/테스트하려면 프로필을 추가로 켭니다:

```bash
docker compose -f docker-compose.dev.yml --profile postgres up -d   # :5432 (kam/kam)
```

### 2) 백엔드 + 프론트엔드

```bash
make migrate    # Alembic 마이그레이션 (SQLite 기본: backend/kam.db)
make seed-dev   # admin 계정(alice) + platform 팀 시드 (멱등)
make backend    # http://localhost:8000  (API 문서: /docs)
make frontend   # http://localhost:5173  (Vite dev 서버)
```

### 3) 로그인 — 개발용 LDAP 계정

| 계정 | 비밀번호 | LDAP 그룹 | 앱에서의 역할 |
|---|---|---|---|
| `alice` | `password` | team-platform, kam-admins | 전역 admin + platform 팀 |
| `bob` | `password` | team-platform | platform 팀 멤버 |
| `carol` | `password` | team-payments | payments 팀 멤버 |

LDAP 그룹은 로그인 시점에 팀으로 동기화됩니다(`kam-admins` → 전역 admin).
팀 매핑/멤버는 앱 관리자 화면에서 수동으로도 관리할 수 있습니다(하이브리드).

### 접속 URL 모음

| 서비스 | URL |
|---|---|
| KAM 웹 UI | http://localhost:5173 |
| KAM API / OpenAPI 문서 | http://localhost:8000 /docs |
| Mailpit (수신 메일 확인) | http://localhost:8025 |
| Prometheus (kind) | http://localhost:30090 |
| Alertmanager (kind) | http://localhost:30093 |

### 정리

```bash
make dev-down        # kind 클러스터 삭제 + compose 종료
make dev-deps-down   # compose만 종료
```

## 설정 (환경변수)

모든 설정은 `KAM_` 접두사의 환경변수입니다(`backend/app/config.py`). 주요 항목:

| 변수 | 기본값 | 설명 |
|---|---|---|
| `KAM_DATABASE_URL` | `sqlite+aiosqlite:///./kam.db` | PostgreSQL: `postgresql+asyncpg://user:pw@host/db` |
| `KAM_SECRET_KEY` | `dev-secret-change-me` | JWT 서명 + 채널 설정 암호화 키. **운영에서 반드시 교체** (기본값이면 기동 시 경고) |
| `KAM_COOKIE_SECURE` | `false` | HTTPS 뒤에서는 `true` 필수 |
| `KAM_JWT_TTL_HOURS` | `12` | 세션 쿠키 수명 |
| `KAM_LDAP_URL` | `ldap://localhost:1389` | + `KAM_LDAP_BIND_DN/PASSWORD`, `KAM_LDAP_USER_BASE/FILTER`, `KAM_LDAP_GROUP_BASE` |
| `KAM_LDAP_ADMIN_GROUPS` | `cn=kam-admins,...` | 전역 admin 그룹 DN — `;` 구분(DN 자체에 `,`가 있으므로) |
| `KAM_DEFAULT_CLUSTER_NAME` | `local` | 최초 기동 시 자동 시드되는 기본 클러스터 |
| `KAM_PROMETHEUS_URL` / `KAM_ALERTMANAGER_URL` | `:30090` / `:30093` | 기본 클러스터의 접속 URL |
| `KAM_WEBHOOK_TOKEN` | `dev-webhook-token` | 기본 클러스터 webhook 토큰. **운영에서 반드시 교체** |
| `KAM_WEBHOOK_BASE_URL` | `http://host.docker.internal:8000` | AM 수신자 설정 스니펫 생성용(클러스터 등록 응답에 1회 표시) |
| `KAM_SMTP_HOST/PORT/...` | Mailpit 기본 | 앱 전역 SMTP 릴레이(채널별 수신자 등은 채널 설정에) |
| `KAM_PLUGINS_DIR` | (비활성) | 서드파티 채널 플러그인 `.py` 스캔 디렉터리 |
| `KAM_APP_BASE_URL` | `http://localhost:5173` | 알림 속 앱 딥링크 생성용 |
| `KAM_WORKER_MODE` | `embedded` | `off`로 두고 `python -m app.worker.runner`를 별도 프로세스로 실행 가능 |

보존 기간(retention), heartbeat 타임아웃 등 런타임 설정은 관리자 UI(설정/클러스터)에서 관리합니다.

## 테스트

```bash
make test            # 백엔드 pytest 전체 + 프론트엔드 빌드
make test-backend    # 백엔드만

# PostgreSQL로 동일 스위트 재실행 (compose postgres 프로필 필요)
cd backend && KAM_DATABASE_URL=postgresql+asyncpg://kam:kam@localhost:5432/kam uv run pytest
```

## 채널 플러그인 개발

`NotificationChannel` ABC(`backend/app/channels/base.py`)를 구현한 `.py` 파일을
`KAM_PLUGINS_DIR`에 두거나 `kam.channels` entry point로 등록하면 채널 타입이 추가됩니다.

- `config_schema`(Pydantic 모델)의 JSON Schema가 API로 노출되어 **프론트엔드가 설정 폼을
  자동 렌더링**합니다 — 플러그인 추가에 프론트 작업이 필요 없습니다.
- 구현 포인트: `send(notification, rendered_message)` 필수,
  `send_batch()`(digest 묶음)와 `send_message()`(리포트 등 알럿 없는 콘텐츠)는 선택
  오버라이드(기본 어댑터 제공).
- 채널 설정은 DB에 Fernet 암호화로 저장됩니다.

참조 구현: [`plugins/example_webhook_channel/`](./plugins/example_webhook_channel/)

## 멀티 클러스터 (개발 시뮬레이션)

kind 클러스터 하나로 멀티 클러스터 경로(클러스터 필터, 클러스터별 webhook 토큰,
클러스터 조건 라우팅 등)를 테스트할 수 있도록, 같은 인프라를 가리키는 두 번째 클러스터
`staging-sim`을 등록하는 멱등 스크립트가 있습니다:

```bash
make dev-second-cluster-sim   # 백엔드 실행 + seed-dev 선행 필요
```

새 webhook 토큰은 **최초 1회만 출력**되므로 기록해 두세요. 물리적으로 분리된 두 번째
kind 클러스터가 필요하면 `dev/kind-config.yaml`을 복제해 NodePort를 바꿔 띄운 뒤
`/admin/clusters`에서 실제 URL로 등록하면 됩니다.

## 배포

컨테이너 이미지(멀티스테이지, 비루트), k8s 매니페스트(최소 권한 RBAC), 클러스터 등록 및
Alertmanager 수신자 설정 절차는 [`deploy/README.md`](./deploy/README.md)를 참고하세요.

```bash
make image           # docker build -> kam:local
make deploy-kind     # kind에 로드 + deploy/k8s 적용 (secret.yaml은 example에서 복사해 작성)
make undeploy-kind
```

**운영 전 필수 확인:**

- `KAM_SECRET_KEY`, `KAM_WEBHOOK_TOKEN` 기본값 교체(기본값 기동 시 경고 로그 발생)
- TLS 종단 뒤라면 `KAM_COOKIE_SECURE=true`
- 멀티 레플리카 HA는 미지원(워커 단일 실행 전제) — `deploy/k8s/deployment.yaml` 주석 참고
