"""P1-8: remember whether a transcript was OK, EMPTY or UNAVAILABLE.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-10-17

Adds three nullable columns to `conversation_session` (transcript_status,
transcript_unavailable_reason, transcript_url). No backfill is possible or wanted: sessions that
already exist keep NULL ("no transcript state recorded"), which the code treats exactly as before.
A fetch that failed in the past was stored as an empty conversation and cannot be told apart now.

Downgrade drops the three columns (the retained signed links go with them).
"""

import sqlalchemy as sa

from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_session", sa.Column("transcript_status", sa.String(16), nullable=True)
    )
    op.add_column(
        "conversation_session",
        sa.Column("transcript_unavailable_reason", sa.String(32), nullable=True),
    )
    op.add_column("conversation_session", sa.Column("transcript_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation_session", "transcript_url")
    op.drop_column("conversation_session", "transcript_unavailable_reason")
    op.drop_column("conversation_session", "transcript_status")
