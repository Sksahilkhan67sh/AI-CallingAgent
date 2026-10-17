"""P1-2: durable retry schedule -- PostgreSQL is the source of truth, Redis a rebuildable cache.

Revision ID: f3a4b5c6d7e8
Revises: e2d1c0b9a8f7
Create Date: 2026-10-17

Adds ONLY three nullable columns on `contact` (next_retry_at, retry_attempt_id,
retry_attempt_number) and one partial index on next_retry_at.

Backfill: every contact already RETRY_SCHEDULED gets next_retry_at = now(), the NEXT attempt
number (attempt_count + 1) and its most recent attempt id, so a schedule that lives only in Redis
today is picked up by the first rebuild. The original due time is unknown (it is only inside the
Redis member), so such a retry is re-checked right away -- the dispatcher still applies
suppression, pause and calling-window checks before anything is dialed.

Downgrade drops the index and the columns; the Redis cache keeps working as before.
"""

import sqlalchemy as sa

from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2d1c0b9a8f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contact", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("contact", sa.Column("retry_attempt_id", sa.Uuid(), nullable=True))
    op.add_column("contact", sa.Column("retry_attempt_number", sa.Integer(), nullable=True))
    op.create_index(
        "ix_contact_next_retry_at",
        "contact",
        ["next_retry_at"],
        postgresql_where=sa.text("next_retry_at IS NOT NULL"),
    )
    op.execute(
        """
        UPDATE contact c
        SET next_retry_at = now(),
            retry_attempt_number = c.attempt_count + 1,
            retry_attempt_id = (
                SELECT a.id FROM call_attempt a
                WHERE a.contact_id = c.id
                ORDER BY a.attempt_number DESC
                LIMIT 1
            )
        WHERE c.status = 'RetryScheduled'
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_contact_next_retry_at",
        table_name="contact",
        postgresql_where=sa.text("next_retry_at IS NOT NULL"),
    )
    op.drop_column("contact", "retry_attempt_number")
    op.drop_column("contact", "retry_attempt_id")
    op.drop_column("contact", "next_retry_at")

