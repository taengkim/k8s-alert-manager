#!/bin/sh
# Container entrypoint: run pending migrations, then start the API server.
# Migrating on every start (rather than as a separate Job/initContainer) is
# deliberate simplicity for this project's scope -- `alembic upgrade head`
# is a no-op once already at head. This is only safe because this
# deployment is documented single-replica (see deploy/k8s/deployment.yaml
# and deploy/README.md): two pods running this entrypoint concurrently
# against an empty database could race the same migration and error out --
# there is no advisory lock around this. Do not scale this Deployment past
# 1 replica without addressing that first.
set -eu

echo "==> running database migrations"
alembic upgrade head

echo "==> starting uvicorn"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
