# Migrations as a one-shot job (P2-5)

## Before [RAN, P2-0]
`backend`, `worker`, `analysis-worker` and `sweeper` all ran `alembic upgrade head` in the shared
entrypoint. Two concurrent runs on a fresh database failed 3 times out of 3: one exited 1 with
`UniqueViolation ... (typname, typnamespace)=(alembic_version, 2200) already exists`.

## Now
1. **`migrate` service** (`docker-compose.yml`): `alembic upgrade head`, `restart: "no"`, waits for a
   healthy `db`. `backend`, `worker`, `analysis-worker` and `sweeper` have
   `depends_on: migrate: condition: service_completed_successfully`.
2. **Entrypoint** migrates only when `RUN_MIGRATIONS=1` (default 0, also in `.env.example`).
3. **Advisory lock** (`app/core/migration_lock.py`, used by `alembic/env.py`): a session-level
   `pg_try_advisory_lock` polled until `DB_MIGRATION_LOCK_WAIT_SECONDS`. A second migrator waits for
   the first, then finds nothing to do. It polls instead of blocking because `lock_timeout` (short
   on purpose for migrations, P2-3) would abort a healthy wait for a slow peer.
4. **Blocked by a long transaction**: each migration is one transaction, so a lock timeout rolls it
   back. `env.py` retries up to `DB_MIGRATION_LOCK_RETRIES` times, then exits non-zero with
   "migration blocked by a lock held by another session ... nothing was half-applied".

## Verified [RAN]
Two concurrent upgrades on a fresh database: both exit 0, one `alembic_version` row equal to the
single head, no duplicate-DDL error (10/10 repeats; failed 3/3 before). A held `ACCESS EXCLUSIVE`
lock: the upgrade aborts cleanly after its retries with the database still at the previous
revision, and succeeds once the blocker releases while it is retrying. A migrator that cannot get
the advisory lock gives up after `DB_MIGRATION_LOCK_WAIT_SECONDS` without creating anything.

## Not verified here
`docker compose config`, and the real start-up ordering of `depends_on: service_completed_successfully`
(no Docker in the build sandbox). The YAML is parsed by a test and the wiring asserted [READ].
HUMAN-TODO: `docker compose config && docker compose up -d` and check `migrate` exits 0 before
`backend`, `worker`, `analysis-worker` and `sweeper` start.
