"""P1-4: contact status NeedsReview (set by the stuck-call sweeper).

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-10-17

Adds ONE value to the `contact_status` enum. No table, column or row changes; nothing to backfill.

PostgreSQL cannot drop an enum value, so the downgrade leaves it in place (an unused value is
harmless) and REFUSES to run while any contact is still NeedsReview: those contacts would otherwise
carry a value the older code cannot read. Resolve them first (reopen or close them).
"""

import sqlalchemy as sa

from alembic import op

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ADD VALUE cannot run inside a transaction block on every supported PostgreSQL version.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE contact_status ADD VALUE IF NOT EXISTS 'NeedsReview'")


def downgrade() -> None:
    stuck = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM contact WHERE status::text = 'NeedsReview'"))
        .scalar_one()
    )
    if stuck:
        raise RuntimeError(
            f"Refusing to downgrade: {stuck} contact(s) are NeedsReview and older code cannot read "
            "that status. Reopen or close them first."
        )
    # The enum value itself stays: PostgreSQL has no ALTER TYPE ... DROP VALUE.
