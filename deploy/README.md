# 배포 (Docker / Kubernetes)

이 문서는 kam을 컨테이너 이미지로 빌드하고, 임의의 쿠버네티스 클러스터에 배포하는
절차를 다룹니다. 로컬 개발 흐름(`make dev-up`, 호스트에서 `make backend`/`make
frontend` 직접 실행)은 이 문서와 무관하며 그대로 유지됩니다 — 자세한 내용은
[루트 README](../README.md)를 참고하세요.

## 사전 준비물

- Docker
- 대상 쿠버네티스 클러스터에 대한 `kubectl` 접근 권한
- (로컬 검증용) `kind`
- LDAP 디렉터리 하나, SMTP 릴레이 하나 — 아래 [LDAP/SMTP 요건](#ldapsmtp-요건) 참고
- kam이 규칙을 생성할 각 클러스터의 Prometheus Operator(PrometheusRule CRD)와
  Alertmanager

## 이미지 빌드

```bash
make image   # docker build -f deploy/Dockerfile -t kam:local .
```

`deploy/Dockerfile`은 멀티스테이지 빌드입니다: `frontend/`를 빌드해 정적 SPA를
만들고, `backend/`의 의존성을 uv로 해석한 뒤, 최종 이미지에 백엔드 앱 + 빌드된
SPA(`backend/app/static/`)를 담습니다. `app.main`은 이 디렉터리가 실제로 존재할
때만 `/`(SPA, `/api/*` 우선)를 서빙합니다 — 로컬 개발(호스트에서 백엔드 실행,
Vite 개발 서버)과 테스트 스위트는 이 디렉터리가 없으므로 영향받지 않습니다.

컨테이너는 비루트 사용자(uid/gid 1000, `kam`)로 실행되고, 엔트리포인트
(`deploy/docker-entrypoint.sh`)가 시작 시 `alembic upgrade head`를 실행한 뒤
`uvicorn`을 띄웁니다.

실제 레지스트리에 배포하려면 태그를 바꿔 푸시하고, `deploy/k8s/deployment.yaml`의
`image:` 필드를 그 레지스트리 참조로 바꾸세요 (이 저장소는 Helm/kustomize 없이
평범한 YAML만 사용합니다 — `image:`는 직접 수정하거나 `kubectl set image`로
갱신).

## 배포 순서

1. **`kam-rules` 네임스페이스를 먼저 만드세요** (kam이 PrometheusRule을 쓰는
   대상 네임스페이스 — `deploy/k8s/rbac.yaml`의 Role이 이 네임스페이스를
   전제합니다. 클러스터마다 다른 `rules_namespace`를 쓰려면 `rbac.yaml`의
   `namespace:`도 함께 바꾸세요):
   ```bash
   kubectl create namespace kam-rules
   ```
2. **시크릿 준비** — `deploy/k8s/secret.example.yaml`을 `secret.yaml`로 복사해
   실제 값(`KAM_SECRET_KEY`, LDAP 바인드 DN/비밀번호, SMTP 자격증명,
   `KAM_DATABASE_URL`)을 채운 뒤 커밋하지 마세요 (`.gitignore`에 이미
   등록되어 있습니다). Postgres가 클러스터 안에 없다면 관리형 DB를 먼저
   준비하거나, 작은 배포라면 `deploy/k8s/postgres.example.yaml`(운영 환경에는
   권장하지 않음 — 파일 자체의 주석 참고)을 적용하세요.
3. **매니페스트 적용** (순서 중요 — Deployment는 ConfigMap/Secret/
   ServiceAccount가 먼저 있어야 합니다):
   ```bash
   kubectl apply -f deploy/k8s/namespace.yaml
   kubectl apply -f deploy/k8s/secret.yaml        # 2번에서 만든 파일
   kubectl apply -f deploy/k8s/configmap.yaml
   kubectl apply -f deploy/k8s/serviceaccount.yaml
   kubectl apply -f deploy/k8s/rbac.yaml
   kubectl apply -f deploy/k8s/deployment.yaml
   kubectl apply -f deploy/k8s/service.yaml
   kubectl -n kam rollout status deployment/kam
   ```
   로컬 kind 클러스터라면 `make deploy-kind`가 이미지 빌드를 제외한 이 전체
   순서(+ `kind load docker-image`)를 대신합니다 (아래 [로컬 kind 검증](#로컬-kind-검증) 참고).
4. **(선택) Ingress** — `deploy/k8s/ingress.example.yaml`을 실제 IngressClass/
   TLS 시크릿에 맞게 고쳐 적용하세요.

## 클러스터 등록 (웹훅 토큰 발급 → 각 클러스터 Alertmanager 설정)

kam은 관리 대상 클러스터마다 별도의 `Cluster` 행을 가집니다. kam이 배포된
클러스터 자신을 포함해, **알림을 받고 싶은 클러스터마다** 아래 절차를
반복하세요:

1. admin 계정으로 로그인 후 `POST /api/v1/clusters`로 클러스터를 등록합니다
   (필드: `name`, `display_name`, `k8s_auth_kind`, `prometheus_url`,
   `alertmanager_url`, `rules_namespace` 등 — `backend/app/api/clusters.py`
   참고). `k8s_auth_kind`는 세 가지입니다:
   - `incluster`: kam 자신이 실행 중인 바로 그 클러스터 — 자격증명 불필요,
     `deploy/k8s/rbac.yaml`의 ServiceAccount 권한을 그대로 씁니다.
   - `kubeconfig`: 원격 클러스터의 kubeconfig YAML을 통째로 저장.
   - `token`: `{"token": "...", "ca_cert": "..."}` — 서비스어카운트 토큰 기반.
2. 응답에 **딱 한 번만** 평문 웹훅 토큰(`webhook_token`)과 그 클러스터의
   Alertmanager에 붙여넣을 리시버 스니펫(`am_config_snippet`)이 들어 있습니다.
   지금 기록해 두세요 — 저장되는 건 해시뿐이라 다시 조회할 수 없습니다
   (토큰을 잃어버렸다면 `PATCH /clusters/{id}`에 `rotate_webhook_token: true`로
   새로 발급).
3. 그 스니펫을 **해당 클러스터의** Alertmanager 설정에 리시버로 추가하세요
   (kube-prometheus-stack이면 Helm values의 `alertmanager.config`, 또는
   `AlertmanagerConfig` CRD). 스니펫의 URL은 `KAM_WEBHOOK_BASE_URL` 기준으로
   렌더링되므로, 그 값이 실제로 **그 클러스터의 Alertmanager에서 kam에 도달
   가능한 주소**인지 미리 확인하세요 — 클러스터마다 네트워크가 다르면
   `am_config_snippet`의 URL을 손으로 고쳐야 할 수 있습니다.
4. Alertmanager 설정을 리로드하면 등록이 끝납니다. `dev/kube-prometheus-values.yaml`이
   로컬 kind 개발 클러스터에 대해 정확히 이 패턴을 씁니다 — 참고하세요.

## LDAP/SMTP 요건

- **LDAP**: 사용자 인증은 전부 LDAP 바인드로 이루어집니다(자체 비밀번호 저장
  없음). `KAM_LDAP_URL`이 가리키는 디렉터리에 `KAM_LDAP_USER_BASE`/
  `KAM_LDAP_USER_FILTER`로 찾을 수 있는 사용자 엔트리와, admin 권한을 부여할
  그룹(`KAM_LDAP_ADMIN_GROUPS`, `;`로 구분된 그룹 DN 목록 — DN 자체가 `,`를
  포함하므로 구분자가 다릅니다)이 있어야 합니다. `KAM_LDAP_BIND_DN`/
  `KAM_LDAP_BIND_PASSWORD`는 그 디렉터리를 읽을 수 있는 서비스 계정입니다.
- **SMTP**: 내장 이메일 채널이 쓰는 발신 릴레이 하나(`KAM_SMTP_HOST`/PORT/
  USERNAME/PASSWORD/STARTTLS). 수신자·제목 등은 채널별 설정이라 여기 없음.
  이 릴레이가 없어도 앱 자체는 뜨지만, 이메일 채널로 알림을 보내는 라우팅
  규칙은 매 시도마다 실패 후 재시도-백오프를 반복하다 결국 dead-letter
  처리됩니다.

## 단일 레플리카 / HA

`deploy/k8s/deployment.yaml`은 의도적으로 `replicas: 1`입니다. 내장 워커
(`KAM_WORKER_MODE=embedded`, 기본값)에 리더 선출/어드바이저리 락이 없고,
컨테이너 엔트리포인트가 매 기동마다 `alembic upgrade head`를 어떤 락도 없이
실행하기 때문입니다 — 그 파일의 주석에 자세한 이유가 있습니다. 이 프로젝트
범위에서 멀티 레플리카 HA는 구현하지 않았습니다.

## 로컬 kind 검증

```bash
make image
make deploy-kind    # kind load docker-image + 위 배포 순서 전체 적용 + rollout 대기
# ... 확인 ...
make undeploy-kind  # kam 네임스페이스 + rbac.yaml/serviceaccount.yaml 리소스 삭제
```

`make deploy-kind`/`make undeploy-kind`는 기본적으로 이름이 `kam`인 kind
클러스터를 대상으로 합니다 (`dev/setup.sh`가 만드는 것과 같은 클러스터) —
`KIND_CLUSTER=<다른이름>` 로 override 가능합니다. `secret.yaml`이 없으면
경고만 출력하고 계속 진행합니다(시크릿 없이는 Deployment가 CrashLoopBackOff에
빠집니다 — 위 [배포 순서](#배포-순서) 2번을 먼저 하세요).

이 두 타깃은 `deploy/k8s/ingress.example.yaml`/`postgres.example.yaml`/
`secret.example.yaml`은 건드리지 않습니다 (전부 opt-in 예시 — 직접
`kubectl apply -f`).
