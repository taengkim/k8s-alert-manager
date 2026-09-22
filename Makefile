.PHONY: dev-deps dev-deps-down dev-up dev-down backend frontend migrate test test-backend

dev-deps:
	docker compose -f docker-compose.dev.yml up -d

dev-deps-down:
	docker compose -f docker-compose.dev.yml down

dev-up:
	docker compose -f docker-compose.dev.yml up -d && ./dev/setup.sh

dev-down:
	./dev/teardown.sh && docker compose -f docker-compose.dev.yml down

backend:
	cd backend && uv run uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

migrate:
	cd backend && uv run alembic upgrade head

test: test-backend
	cd frontend && npm run build

test-backend:
	cd backend && uv run pytest
