"""cp14b: analysis lease/fencing, retry scheduling, daily budget ledger

ADDITIVE ONLY. Nothing is dropped or rewritten except one deliberate recovery step:

* analysis_status gains `retry_wait` and `skipped` (ALTER TYPE ... ADD VALUE, run in an
  autocommit block -- PostgreSQL cannot use a new enum value in the transaction that adds it,
  and this migration does not).
* call_analysis gains claim_token / lease_expires_at (fencing + lease), next_attempt_at,
  last_enqueued_at, truncated, and the estimated-spend columns. All nullable or defaulted, so
  existing rows and the existing read API stay valid.
* Rows left `processing` by a pre-CP14B (lease-less) worker are given an ALREADY-EXPIRED lease
  and a fresh token, so the new CHECK constraint holds and the sweeper recovers them. No row is
  deleted or moved to a terminal state.
* analysis_budget_day is a new table (durable estimated-spend ledger).
* call_attempt gains dograh_workflow_id (nullable).

DOWNGRADE: drops only what this migration added. PostgreSQL cannot remove enum values, so
`retry_wait` / `skipped` remain in the type (harmless). Downgrade REFUSES to run while any
call_analysis row is in `retry_wait` or `skipped`, because older code does not know those
states and the rows would be stranded -- resolve them deliberately first (forward-fix is the
safe path; see docs/CHECKPOINT-14B-NOTES.md).

Revision ID: a83d13f0c415
Revises: c14b4e5f6a7b
Create Date: 2026-10-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a83d13f0c415'
down_revision: Union[str, None] = 'c14b4e5f6a7b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE analysis_status ADD VALUE IF NOT EXISTS 'retry_wait'")
        op.execute("ALTER TYPE analysis_status ADD VALUE IF NOT EXISTS 'skipped'")

    op.add_column('call_analysis', sa.Column('claim_token', sa.Uuid(), nullable=True))
    op.add_column('call_analysis', sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('call_analysis', sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('call_analysis', sa.Column('last_enqueued_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('call_analysis', sa.Column('truncated', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('call_analysis', sa.Column('budget_day', sa.Date(), nullable=True))
    op.add_column('call_analysis', sa.Column('reserved_cost', sa.Numeric(12, 6), nullable=True))
    op.add_column('call_analysis', sa.Column('observed_run_cost_usd', sa.Numeric(12, 6), nullable=True))

    # Recover lease-less PROCESSING rows (see module docstring) before the CHECK is added.
    op.execute(
        "UPDATE call_analysis SET claim_token = gen_random_uuid(), lease_expires_at = now() "
        "WHERE status = 'processing' AND (claim_token IS NULL OR lease_expires_at IS NULL)"
    )
    op.create_check_constraint(
        'ck_call_analysis_processing_has_lease', 'call_analysis',
        "status <> 'processing' OR (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        'ck_call_analysis_attempt_count_nonneg', 'call_analysis', 'attempt_count >= 0'
    )
    op.create_index('ix_call_analysis_status_next_attempt', 'call_analysis', ['status', 'next_attempt_at'])
    op.create_index('ix_call_analysis_status_lease', 'call_analysis', ['status', 'lease_expires_at'])

    op.create_table(
        'analysis_budget_day',
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('reserved_cost', sa.Numeric(12, 6), server_default='0', nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('day'),
    )
    op.add_column('call_attempt', sa.Column('dograh_workflow_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    stranded = op.get_bind().execute(
        sa.text("SELECT count(*) FROM call_analysis WHERE status IN ('retry_wait', 'skipped')")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Refusing to downgrade: {stranded} call_analysis row(s) are in 'retry_wait' or "
            "'skipped', which pre-CP14B code does not understand. Resolve them deliberately "
            "(forward-fix) before downgrading; nothing is deleted automatically."
        )
    op.drop_column('call_attempt', 'dograh_workflow_id')
    op.drop_table('analysis_budget_day')
    op.drop_index('ix_call_analysis_status_lease', table_name='call_analysis')
    op.drop_index('ix_call_analysis_status_next_attempt', table_name='call_analysis')
    op.drop_constraint('ck_call_analysis_attempt_count_nonneg', 'call_analysis', type_='check')
    op.drop_constraint('ck_call_analysis_processing_has_lease', 'call_analysis', type_='check')
    for col in ('observed_run_cost_usd', 'reserved_cost', 'budget_day', 'truncated',
                'last_enqueued_at', 'next_attempt_at', 'lease_expires_at', 'claim_token'):
        op.drop_column('call_analysis', col)
