# k8s-alert-manager (kam)

멀티팀, 멀티클러스터 환경을 위한 Kubernetes 알림 관리 웹 애플리케이션입니다.

- 백엔드: FastAPI + async SQLAlchemy + Alembic
- 프론트엔드: React 19 + Vite + antd 5

전체 설계 문서는 [`docs/design.md`](./docs/design.md)를 참고하세요.

## Quickstart

```bash
make dev-deps      # 개발용 의존 서비스 기동 (openldap, mailpit, ...)
make backend       # 백엔드 개발 서버 실행 (http://localhost:8000)
make frontend      # 프론트엔드 개발 서버 실행 (Vite)
make test          # 백엔드 + 프론트엔드 테스트/빌드 실행
```

개발 의존 서비스를 종료하려면:

```bash
make dev-deps-down
```

DB 마이그레이션 적용:

```bash
make migrate
```

개발용 admin 계정(alice)과 platform 팀을 시드(멱등, 마이그레이션 포함):

```bash
make seed-dev
```

## 멀티 클러스터 (개발)

로컬 kind 클러스터 하나로 멀티 클러스터 UI/API 경로(ClusterFilter, `/admin/clusters`,
클러스터별 webhook 토큰, `clusters=[...]`로 범위를 좁힌 라우팅 규칙 등)를 테스트할 수
있도록, 같은 인프라(같은 Prometheus/Alertmanager/kubeconfig)를 가리키는 `staging-sim`
클러스터를 등록하는 멱등 스크립트가 있습니다:

```bash
make dev-second-cluster-sim
```

백엔드가 실행 중이어야 하며(`make backend`), `seed-dev`로 만든 admin 계정으로 로그인해
클러스터 생성 API를 호출합니다. 새 웹훅 토큰은 최초 1회만 출력되므로 꼭 기록해 두세요.

실제로 물리적으로 분리된 두 번째 클러스터(`dev-up-2` 같은 별도 kind 클러스터)를 붙이는
것은 이번 단계 범위 밖입니다. 필요하다면 `dev/kind-config.yaml`을 참고해 두 번째
kind 클러스터를 별도 이름으로 띄우고, `kube-prometheus-values.yaml`의 NodePort를
충돌하지 않게 바꾼 뒤, `/admin/clusters`에서 그 클러스터의 실제 Prometheus/Alertmanager
URL로 새 클러스터를 등록하면 됩니다.

## 배포

컨테이너 이미지 빌드, 쿠버네티스 매니페스트, 클러스터 등록 절차는
[`deploy/README.md`](./deploy/README.md)를 참고하세요.
