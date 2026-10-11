"""cp15a: data lifecycle -- legal hold, purge markers, erasure_log, retention_run, scan indexes

Schema only. No data is rewritten except one conservative backfill: `campaign.closed_at` for
campaigns that are already COMPLETED is taken from their `campaign.status_changed` audit row;
a COMPLETED campaign with no such row keeps closed_at NULL and is therefore never auto-purged
(a missing date must never shorten a retention period).

* campaign: legal_hold (default false), closed_at
* contact: legal_hold (default false), purged_at
* erasure_log: append-only (UPDATE/DELETE rejected by trigger)
* retention_run
* indexes for retention scans: conversation_message.created_at, processed_event.received_at,
  working_memory_snapshot.snapshotted_at

Downgrade REFUSES to run while erasure_log has rows or any legal hold is set: dropping them
would silently destroy erasure evidence / lift a hold. Resolve deliberately, then re-run.

Revision ID: c15a1b2c3d4e
Revises: a83d13f0c415
Create Date: 2026-10-10 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'c15a1b2c3d4e'
down_revision: Union[str, None] = 'a83d13f0c415'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('campaign', sa.Column('legal_hold', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('campaign', sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('contact', sa.Column('legal_hold', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('contact', sa.Column('purged_at', sa.DateTime(timezone=True), nullable=True))

    op.execute(
        """
        UPDATE campaign c SET closed_at = sub.closed
        FROM (
            SELECT entity_id, max(created_at) AS closed
            FROM audit_log
            WHERE action = 'campaign.status_changed'
              AND entity_type = 'campaign'
              AND metadata->>'to' = 'completed'
            GROUP BY entity_id
        ) sub
        WHERE c.id = sub.entity_id AND c.status = 'completed'
        """
    )

    op.create_table(
        'erasure_log',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('requested_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('actor', sa.String(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('counts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_erasure_log_fingerprint', 'erasure_log', ['fingerprint'], unique=False)
    op.execute(
        """
        CREATE FUNCTION erasure_log_append_only() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'erasure_log is append-only';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER erasure_log_append_only BEFORE UPDATE OR DELETE ON erasure_log "
        "FOR EACH ROW EXECUTE FUNCTION erasure_log_append_only()"
    )

    op.create_table(
        'retention_run',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('mode', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('counts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('error_category', sa.String(), nullable=True),
        sa.CheckConstraint("mode IN ('dry_run', 'apply')", name='ck_retention_run_mode'),
        sa.CheckConstraint("status IN ('running', 'completed', 'partial', 'failed')", name='ck_retention_run_status'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_retention_run_started_at', 'retention_run', ['started_at'], unique=False)

    op.create_index('ix_conversation_message_created_at', 'conversation_message', ['created_at'], unique=False)
    op.create_index('ix_processed_event_received_at', 'processed_event', ['received_at'], unique=False)
    op.create_index('ix_working_memory_snapshot_snapshotted_at', 'working_memory_snapshot', ['snapshotted_at'], unique=False)


def downgrade() -> None:
    conn = op.get_bind()
    logged = conn.execute(sa.text("SELECT count(*) FROM erasure_log")).scalar_one()
    holds = conn.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM campaign WHERE legal_hold) "
            "+ (SELECT count(*) FROM contact WHERE legal_hold)"
        )
    ).scalar_one()
    if logged or holds:
        raise RuntimeError(
            f"Refusing to downgrade: {logged} erasure_log row(s) and {holds} active legal "
            "hold(s) would be destroyed. Export the erasure log (it is append-only: deleting it "
            "needs a deliberate `ALTER TABLE erasure_log DISABLE TRIGGER erasure_log_append_only`), "
            "release the holds deliberately, then re-run. Nothing has been changed."
        )

    op.drop_index('ix_working_memory_snapshot_snapshotted_at', table_name='working_memory_snapshot')
    op.drop_index('ix_processed_event_received_at', table_name='processed_event')
    op.drop_index('ix_conversation_message_created_at', table_name='conversation_message')
    op.drop_index('ix_retention_run_started_at', table_name='retention_run')
    op.drop_table('retention_run')
    op.execute("DROP TRIGGER erasure_log_append_only ON erasure_log")
    op.execute("DROP FUNCTION erasure_log_append_only()")
    op.drop_index('ix_erasure_log_fingerprint', table_name='erasure_log')
    op.drop_table('erasure_log')
    op.drop_column('contact', 'purged_at')
    op.drop_column('contact', 'legal_hold')
    op.drop_column('campaign', 'closed_at')
    op.drop_column('campaign', 'legal_hold')
