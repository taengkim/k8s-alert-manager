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
