#!/bin/sh
# Apply pending Alembic migrations before the app starts. Schema changes
# go through migrations, not Base.metadata.create_all() -- see
# app/core/database.py and Checkpoint 01 Step 4.
#
# Checkpoint 09: only ONE container should migrate. The API container does
# (default); workers set RUN_MIGRATIONS=false and wait for the API to be
# healthy, so concurrent `alembic upgrade head` runs cannot race.
set -e

if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
  alembic upgrade head
fi
exec "$@"
