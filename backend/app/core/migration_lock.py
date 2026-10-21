"""Serialise concurrent `alembic upgrade` runs with a PostgreSQL advisory lock (P2-5).

Several containers used to run `alembic upgrade head` at start-up; on a fresh database two of them
raced to create `alembic_version` and one crashed. A session-level advisory lock makes the second
migrator wait for the first, then find nothing left to do. The wait is a bounded poll rather than
a blocking `pg_advisory_lock`, because `lock_timeout` (deliberately short for migrations) would
otherwise abort a healthy wait for a slow peer.
"""

import time

from sqlalchemy import text
from sqlalchemy.engine import Connection

# Arbitrary fixed key, unique to this application's migrations.
MIGRATION_LOCK_KEY = 7_240_050_001


class MigrationLockTimeout(RuntimeError):
    """Another migrator held the lock for the whole wait."""


def acquire_migration_lock(
    connection: Connection, wait_seconds: float, poll_seconds: float = 0.5
) -> None:
    deadline = time.monotonic() + wait_seconds
    while True:
        got = connection.execute(
            text("select pg_try_advisory_lock(:k)"), {"k": MIGRATION_LOCK_KEY}
        ).scalar_one()
        connection.commit()  # never sit in a transaction while waiting
        if got:
            return
        if time.monotonic() >= deadline:
            raise MigrationLockTimeout(
                f"another migration has held the migration lock for more than {wait_seconds:g} s "
                "(DB_MIGRATION_LOCK_WAIT_SECONDS); not migrating"
            )
        time.sleep(poll_seconds)


def release_migration_lock(connection: Connection) -> None:
    """Best effort: closing the connection releases a session lock anyway."""
    try:
        connection.rollback()
        connection.execute(text("select pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_KEY})
        connection.commit()
    except Exception:  # noqa: BLE001, S110
        pass
