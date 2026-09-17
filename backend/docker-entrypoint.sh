#!/bin/sh
# Apply pending Alembic migrations before the app starts. Schema changes
# go through migrations, not Base.metadata.create_all() -- see
# app/core/database.py and Checkpoint 01 Step 4.
set -e

alembic upgrade head
exec "$@"
