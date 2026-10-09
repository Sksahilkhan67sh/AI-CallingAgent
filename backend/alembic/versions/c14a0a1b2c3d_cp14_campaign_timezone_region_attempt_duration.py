"""cp14: campaign timezone + default_region, call_attempt duration + started_at index

Schema only, additive and reversible. Existing campaigns get the single-tenant defaults
(Asia/Kolkata / IN) through the server default; no data is rewritten here.

Revision ID: c14a0a1b2c3d
Revises: d15c334bf7e7
Create Date: 2026-10-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c14a0a1b2c3d'
down_revision: Union[str, None] = 'd15c334bf7e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'campaign',
        sa.Column('timezone', sa.String(), server_default=sa.text("'Asia/Kolkata'"), nullable=False),
    )
    op.add_column(
        'campaign',
        sa.Column('default_region', sa.String(), server_default=sa.text("'IN'"), nullable=False),
    )
    op.add_column('call_attempt', sa.Column('duration_seconds', sa.Float(), nullable=True))
    op.create_index('ix_call_attempt_started_at', 'call_attempt', ['started_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_call_attempt_started_at', table_name='call_attempt')
    op.drop_column('call_attempt', 'duration_seconds')
    op.drop_column('campaign', 'default_region')
    op.drop_column('campaign', 'timezone')
