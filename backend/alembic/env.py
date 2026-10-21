"""Alembic environment.

Reads DATABASE_URL from application Settings (not a hardcoded URL in
alembic.ini), and uses Base.metadata for autogenerate.
"""

import logging
import time
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.exc import OperationalError

from app.core.config import get_settings
from app.core.database import migration_timeouts_ms, server_options
from app.core.migration_lock import (
    MigrationLockTimeout,
    acquire_migration_lock,
    release_migration_lock,
)
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().primary_db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


_LOCK_NOT_AVAILABLE = "55P03"
log = logging.getLogger("alembic.env")


def _migrate_once(connectable, wait_seconds: int) -> None:
    with connectable.connect() as connection:
        acquire_migration_lock(connection, wait_seconds)  # P2-5: one migrator at a time
        try:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            release_migration_lock(connection)


def run_migrations_online() -> None:
    settings = get_settings()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"options": server_options(migration_timeouts_ms(settings))},
    )
    attempts = settings.db_migration_lock_retries
    for attempt in range(1, attempts + 1):
        try:
            _migrate_once(connectable, settings.db_migration_lock_wait_seconds)
            return
        except MigrationLockTimeout as exc:
            raise SystemExit(str(exc)) from None
        except OperationalError as exc:
            if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            # A long transaction holds a lock this migration needs. Each migration is one
            # transaction, so the failed one was rolled back; try again, then give up loudly.
            if attempt == attempts:
                raise SystemExit(
                    f"migration blocked by a lock held by another session ({attempts} attempts, "
                    f"DB_MIGRATION_LOCK_TIMEOUT_MS={settings.db_migration_lock_timeout_ms}); "
                    "nothing was half-applied, find and end the blocking transaction and rerun"
                ) from None
            log.warning("migration blocked by a lock (attempt %d/%d); retrying", attempt, attempts)
            time.sleep(settings.db_migration_retry_delay_seconds)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
