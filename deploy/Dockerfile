# syntax=docker/dockerfile:1
#
# Multi-stage build for k8s-alert-manager. Build context MUST be the repo
# root (both `frontend/` and `backend/` are referenced below), e.g.:
#
#   docker build -f deploy/Dockerfile -t kam:local .
#
# (see `make image`, which does exactly this).

########################################
# Stage 1: build the frontend SPA
########################################
FROM node:24-alpine AS frontend-builder
WORKDIR /frontend
# Dependencies first so this layer only re-runs when package*.json changes,
# not on every source edit.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

########################################
# Stage 2: resolve backend dependencies into a venv
########################################
FROM python:3.12-slim AS backend-builder
# Official documented pattern for pulling the standalone uv binary into an
# image without a separate install script/curl step -- see
# https://docs.astral.sh/uv/guides/integration/docker/.
COPY --from=ghcr.io/astral-sh/uv:0.11.22 /uv /uvx /usr/local/bin/
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=python3.12 \
    UV_PYTHON_DOWNLOADS=never
# UV_PYTHON pins uv to this image's own python3.12 rather than resolving
# whatever `requires-python` happens to prefer; UV_PYTHON_DOWNLOADS=never
# makes that a hard failure (not a silent network fetch of a managed
# interpreter) if that ever stops matching -- this build has no business
# downloading a Python at all when python:3.12-slim already provides one.
# backend/pyproject.toml declares no [build-system] -- this is a dependency
# manifest only (no installable "kam-backend" package), so `uv sync` here
# needs no application source at all, just the lock. That keeps this layer
# cached across every app code change; only a dependency change invalidates
# it.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

########################################
# Stage 3: final runtime image
########################################
FROM python:3.12-slim AS final

# Non-root app user, fixed uid/gid 1000 (matches deploy/k8s/deployment.yaml's
# securityContext, which pins runAsUser/runAsGroup to the same value rather
# than relying on whatever this build happens to assign).
RUN groupadd --gid 1000 kam \
    && useradd --uid 1000 --gid kam --create-home --shell /usr/sbin/nologin kam

WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=backend-builder /app/.venv ./.venv
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./
# The SPA build output lands under app/static/ -- app.main only mounts the
# StaticFiles/SPA-fallback route when this directory exists (see
# app/main.py's STATIC_DIR), so a source checkout without this stage still
# runs the API fine with no frontend to serve.
COPY --from=frontend-builder /frontend/dist ./app/static

COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && chown -R kam:kam /app

USER kam
EXPOSE 8000

# See deploy/README.md and app/config.py for the full documented KAM_* env
# surface (KAM_DATABASE_URL, KAM_SECRET_KEY, KAM_LDAP_*, KAM_SMTP_*, ...).
ENTRYPOINT ["docker-entrypoint.sh"]
