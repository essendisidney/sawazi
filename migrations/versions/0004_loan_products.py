"""loan products

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07 01:42:52.151503
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('loan_products',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('code', sa.String(length=20), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('min_amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('max_amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('max_term_months', sa.Integer(), nullable=False),
    sa.Column('interest_rate_bps', sa.Integer(), nullable=False),
    sa.Column('interest_method', sa.String(length=10), nullable=False),
    sa.Column('deposits_multiplier_pct', sa.Integer(), nullable=False),
    sa.Column('min_membership_months', sa.Integer(), nullable=False),
    sa.Column('max_arrears_days', sa.Integer(), nullable=False),
    sa.Column('one_third_rule', sa.Boolean(), nullable=False),
    sa.Column('guarantor_cover', sa.String(length=20), nullable=False),
    sa.Column('min_guarantors', sa.Integer(), nullable=False),
    sa.Column('second_approval_above_cents', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'code')
    )
    with op.batch_alter_table('loan_products', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_loan_products_institution_id'), ['institution_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('loan_products', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_loan_products_institution_id'))

    op.drop_table('loan_products')
