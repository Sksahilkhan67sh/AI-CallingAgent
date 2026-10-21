#!/bin/sh
# Optionally apply pending Alembic migrations, then start the app.
#
# P2-5: migrations normally run ONCE, in the compose `migrate` service, and the other services
# wait for it. Four containers each running `alembic upgrade head` raced on a fresh database.
# Set RUN_MIGRATIONS=1 only for a single-container deployment that has no migrate job.
# Schema changes go through migrations, not Base.metadata.create_all() -- see
# app/core/database.py and Checkpoint 01 Step 4.
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    alembic upgrade head
fi
exec "$@"
