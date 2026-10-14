"""loan arrears history: each loan's arrears at every snapshot, for roll rates

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-14 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('loan_arrears_history',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('as_of', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('days_in_arrears', sa.Integer(), nullable=False),
    sa.Column('balance_cents', sa.BigInteger(), nullable=False),
    sa.Column('arrears_cents', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('loan_id', 'as_of')
    )
    op.create_index(op.f('ix_loan_arrears_history_as_of'), 'loan_arrears_history', ['as_of'], unique=False)
    op.create_index(op.f('ix_loan_arrears_history_institution_id'), 'loan_arrears_history', ['institution_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_loan_arrears_history_institution_id'), table_name='loan_arrears_history')
    op.drop_index(op.f('ix_loan_arrears_history_as_of'), table_name='loan_arrears_history')
    op.drop_table('loan_arrears_history')
