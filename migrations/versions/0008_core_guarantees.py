"""core guarantees

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-07 12:18:59.555593
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('core_guarantees',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('guarantor_member_id', sa.Integer(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('imported_at', sa.DateTime(), nullable=False),
    sa.Column('released_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['guarantor_member_id'], ['members.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'loan_id', 'guarantor_member_id')
    )
    with op.batch_alter_table('core_guarantees', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_core_guarantees_guarantor_member_id'), ['guarantor_member_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_core_guarantees_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_core_guarantees_loan_id'), ['loan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_core_guarantees_status'), ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('core_guarantees', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_core_guarantees_status'))
        batch_op.drop_index(batch_op.f('ix_core_guarantees_loan_id'))
        batch_op.drop_index(batch_op.f('ix_core_guarantees_institution_id'))
        batch_op.drop_index(batch_op.f('ix_core_guarantees_guarantor_member_id'))

    op.drop_table('core_guarantees')
