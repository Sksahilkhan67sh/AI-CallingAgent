"""HF1: durable outbound controls (kill switch / provider halt) + one missing FK index.

Revision ID: e2d1c0b9a8f7
Revises: c15a1b2c3d4e
Create Date: 2026-10-14

Adds ONLY:
* `outbound_control` -- PostgreSQL becomes the source of truth for the kill switch (and the
  automatic provider halt). Rows are created lazily; an empty table means "nothing engaged".
* `ix_call_analysis_conversation_session_id` -- the FK from call_analysis to
  conversation_session had no index.

Nothing is backfilled: a kill switch engaged only in Redis before this deploy is NOT carried
over (re-engage it through the API right after deploying -- see docs/HARDENING-HF1-NOTES.md).

Downgrade REFUSES to run while a control is engaged: dropping the table would silently lift a
stop. Release it first (API or `UPDATE outbound_control SET engaged = false`).
"""

import sqlalchemy as sa

from alembic import op

revision = "e2d1c0b9a8f7"
down_revision = "c15a1b2c3d4e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbound_control",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("engaged", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("engaged_by", sa.Text(), nullable=True),
        sa.Column("engaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "ix_call_analysis_conversation_session_id",
        "call_analysis",
        ["conversation_session_id"],
    )


def downgrade() -> None:
    engaged = (
        op.get_bind()
        .execute(sa.text("SELECT key FROM outbound_control WHERE engaged ORDER BY key"))
        .scalars()
        .all()
    )
    if engaged:
        raise RuntimeError(
            "Refusing to downgrade: these outbound controls are still engaged and dropping "
            f"the table would silently lift them: {', '.join(engaged)}. Release them first."
        )
    op.drop_index("ix_call_analysis_conversation_session_id", table_name="call_analysis")
    op.drop_table("outbound_control")
