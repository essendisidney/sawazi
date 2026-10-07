"""ussd sessions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-07 11:45:30.166584
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('ussd_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.String(length=100), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('guarantee_ids', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('session_id')
    )
    with op.batch_alter_table('ussd_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ussd_sessions_created_at'), ['created_at'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('ussd_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ussd_sessions_created_at'))

    op.drop_table('ussd_sessions')
