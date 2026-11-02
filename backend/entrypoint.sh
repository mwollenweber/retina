#!/bin/sh
set -e
# Single-replica service: running migrate on boot is safe. If you scale out,
# move this to a one-off ECS run-task (see infra/README.md).
if [ "${SKIP_MIGRATE:-0}" != "1" ]; then
  python manage.py migrate --noinput
fi
exec "$@"
