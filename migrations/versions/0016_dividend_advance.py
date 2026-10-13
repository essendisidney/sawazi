"""dividend advance: members' last dividend, products that lend against it

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-13 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('members', sa.Column('last_dividend_cents', sa.BigInteger(), nullable=True))
    op.add_column('loan_products', sa.Column('dividend_advance_pct', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('loan_products', 'dividend_advance_pct')
    op.drop_column('members', 'last_dividend_cents')
