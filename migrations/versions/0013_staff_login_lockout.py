"""staff login lockout: wrong passwords in a row, and until when the account waits

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-12 17:57:40.732712
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('failed_logins', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('locked_until', sa.DateTime(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.drop_column('locked_until')
        batch_op.drop_column('failed_logins')

