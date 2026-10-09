"""staff choose their own password after someone else set it; institutions can be marked as demos

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-09 14:07:08.746927
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('institutions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_demo', sa.Boolean(), server_default=sa.false(), nullable=False))

    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('must_change_password', sa.Boolean(), server_default=sa.false(), nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.drop_column('must_change_password')

    with op.batch_alter_table('institutions', schema=None) as batch_op:
        batch_op.drop_column('is_demo')

