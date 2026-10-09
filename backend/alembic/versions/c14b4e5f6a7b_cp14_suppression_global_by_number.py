"""cp14: suppression becomes a global do-not-call table keyed by number

Before: PRIMARY KEY (contact_id) -- a number without a contact could not be stored.
After:  PRIMARY KEY (id); contact_id NULLABLE (FK kept, unique when present);
        phone_number UNIQUE globally; created_by added.

Nothing is dropped silently:
* upgrade REFUSES to run if two rows share a phone_number (the unique constraint cannot be
  added, and choosing which row to keep is a data decision, not a schema one). Resolve with
  `python -m app.scripts.dedupe_suppressions` (dry run by default, `--apply` to write; keeps
  the earliest row and reports the rest), then re-run the migration.
* downgrade REFUSES to run while any row has no contact_id (the old primary key cannot hold
  it). Those rows are the new capability; remove or link them deliberately first.

Existing rows keep their data and receive a generated id.

Revision ID: c14b4e5f6a7b
Revises: c14a0a1b2c3d
Create Date: 2026-10-08 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c14b4e5f6a7b'
down_revision: Union[str, None] = 'c14a0a1b2c3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            "SELECT count(*) FROM ("
            " SELECT phone_number FROM suppression GROUP BY phone_number HAVING count(*) > 1"
            ") d"
        )
    ).scalar_one()
    if duplicates:
        raise RuntimeError(
            f"{duplicates} phone number(s) appear more than once in suppression; the global "
            "unique constraint cannot be added. Run `python -m app.scripts.dedupe_suppressions` "
            "(dry run), review it, run it again with --apply, then re-run this migration. "
            "Nothing has been changed."
        )

    op.add_column('suppression', sa.Column('id', sa.Uuid(), nullable=True))
    op.execute("UPDATE suppression SET id = gen_random_uuid()")
    op.alter_column('suppression', 'id', nullable=False)
    op.drop_constraint('suppression_pkey', 'suppression', type_='primary')
    op.create_primary_key('suppression_pkey', 'suppression', ['id'])
    op.alter_column('suppression', 'contact_id', existing_type=sa.Uuid(), nullable=True)
    op.add_column('suppression', sa.Column('created_by', sa.String(), nullable=True))
    op.drop_index('ix_suppression_phone_number', table_name='suppression')
    op.create_unique_constraint('uq_suppression_phone_number', 'suppression', ['phone_number'])
    op.create_unique_constraint('uq_suppression_contact_id', 'suppression', ['contact_id'])


def downgrade() -> None:
    conn = op.get_bind()
    orphans = conn.execute(
        sa.text("SELECT count(*) FROM suppression WHERE contact_id IS NULL")
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"{orphans} suppression row(s) have no contact and cannot be represented by the "
            "old schema (PRIMARY KEY contact_id). Remove or link them deliberately before "
            "downgrading; nothing has been changed."
        )

    op.drop_constraint('uq_suppression_contact_id', 'suppression', type_='unique')
    op.drop_constraint('uq_suppression_phone_number', 'suppression', type_='unique')
    op.create_index('ix_suppression_phone_number', 'suppression', ['phone_number'], unique=False)
    op.drop_column('suppression', 'created_by')
    op.drop_constraint('suppression_pkey', 'suppression', type_='primary')
    op.alter_column('suppression', 'contact_id', existing_type=sa.Uuid(), nullable=False)
    op.create_primary_key('suppression_pkey', 'suppression', ['contact_id'])
    op.drop_column('suppression', 'id')
