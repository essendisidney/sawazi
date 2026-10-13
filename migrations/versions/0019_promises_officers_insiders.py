"""promises to pay, loan officers, insiders

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-13 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('promises_to_pay',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('made_on', sa.Date(), nullable=False),
    sa.Column('due_on', sa.Date(), nullable=False),
    sa.Column('note', sa.String(length=500), nullable=True),
    sa.Column('recorded_by_user_id', sa.Integer(), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.ForeignKeyConstraint(['recorded_by_user_id'], ['staff_users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_promises_to_pay_institution_id'), 'promises_to_pay', ['institution_id'], unique=False)
    op.create_index(op.f('ix_promises_to_pay_loan_id'), 'promises_to_pay', ['loan_id'], unique=False)
    op.add_column('loans', sa.Column('officer', sa.String(length=120), nullable=True))
    op.add_column('members', sa.Column('insider', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('members', 'insider')
    op.drop_column('loans', 'officer')
    op.drop_index(op.f('ix_promises_to_pay_loan_id'), table_name='promises_to_pay')
    op.drop_index(op.f('ix_promises_to_pay_institution_id'), table_name='promises_to_pay')
    op.drop_table('promises_to_pay')
