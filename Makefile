.PHONY: dev-deps dev-deps-down dev-up dev-down backend frontend migrate seed-dev dev-second-cluster-sim test test-backend image deploy-kind undeploy-kind

KIND_CLUSTER ?= kam
IMAGE ?= kam:local

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

seed-dev:
	cd backend && uv run python -m scripts.seed_dev

dev-second-cluster-sim:
	cd backend && uv run python -m scripts.add_sim_cluster

test: test-backend
	cd frontend && npm run build

test-backend:
	cd backend && uv run pytest

# See deploy/README.md for the full packaging/deploy story (image build,
# manifest apply order, cluster registration, LDAP/SMTP requirements).

image:
	docker build -f deploy/Dockerfile -t $(IMAGE) .

deploy-kind:
	kind load docker-image $(IMAGE) --name $(KIND_CLUSTER)
	kubectl apply -f deploy/k8s/namespace.yaml
	@if [ -f deploy/k8s/secret.yaml ]; then \
		kubectl apply -f deploy/k8s/secret.yaml; \
	else \
		echo "WARNING: deploy/k8s/secret.yaml not found -- copy deploy/k8s/secret.example.yaml and fill in real values (see deploy/README.md). Deploying without it; the pod will crash-loop until it exists."; \
	fi
	kubectl apply -f deploy/k8s/configmap.yaml
	kubectl apply -f deploy/k8s/serviceaccount.yaml
	kubectl apply -f deploy/k8s/rbac.yaml
	kubectl apply -f deploy/k8s/deployment.yaml
	kubectl apply -f deploy/k8s/service.yaml
	kubectl -n kam rollout status deployment/kam --timeout=180s

undeploy-kind:
	kubectl delete -f deploy/k8s/service.yaml -f deploy/k8s/deployment.yaml --ignore-not-found
	kubectl delete -f deploy/k8s/rbac.yaml -f deploy/k8s/serviceaccount.yaml --ignore-not-found
	kubectl delete -f deploy/k8s/configmap.yaml --ignore-not-found
	kubectl delete -f deploy/k8s/secret.yaml --ignore-not-found
	kubectl delete -f deploy/k8s/namespace.yaml --ignore-not-found
