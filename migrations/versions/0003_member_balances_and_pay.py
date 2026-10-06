"""member balances and pay

Optional figures from the core system and payroll, used by loan appraisal. All nullable: unknown is not zero.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07 01:34:48.932990
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('members', schema=None) as batch_op:
        batch_op.add_column(sa.Column('joined_on', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('deposits_cents', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('shares_cents', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('balances_as_of', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('gross_pay_cents', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('net_pay_cents', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('pay_as_of', sa.Date(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('members', schema=None) as batch_op:
        batch_op.drop_column('pay_as_of')
        batch_op.drop_column('net_pay_cents')
        batch_op.drop_column('gross_pay_cents')
        batch_op.drop_column('balances_as_of')
        batch_op.drop_column('shares_cents')
        batch_op.drop_column('deposits_cents')
        batch_op.drop_column('joined_on')

