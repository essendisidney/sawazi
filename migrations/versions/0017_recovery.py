"""recovery: defaulted loans with agents, guarantors' deposits used and refunded

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-13 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('recoveries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('stage', sa.String(length=20), nullable=False),
    sa.Column('agent', sa.String(length=200), nullable=True),
    sa.Column('handed_over_on', sa.Date(), nullable=True),
    sa.Column('recovered_cents', sa.BigInteger(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'loan_id')
    )
    op.create_index(op.f('ix_recoveries_institution_id'), 'recoveries', ['institution_id'], unique=False)
    op.create_table('recovery_guarantors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('recovery_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('used_cents', sa.BigInteger(), nullable=False),
    sa.Column('used_on', sa.Date(), nullable=False),
    sa.Column('refunded_cents', sa.BigInteger(), nullable=False),
    sa.Column('refunded_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.ForeignKeyConstraint(['recovery_id'], ['recoveries.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recovery_id', 'member_id')
    )
    op.create_index(op.f('ix_recovery_guarantors_institution_id'), 'recovery_guarantors', ['institution_id'], unique=False)
    op.create_index(op.f('ix_recovery_guarantors_member_id'), 'recovery_guarantors', ['member_id'], unique=False)
    op.create_index(op.f('ix_recovery_guarantors_recovery_id'), 'recovery_guarantors', ['recovery_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_recovery_guarantors_recovery_id'), table_name='recovery_guarantors')
    op.drop_index(op.f('ix_recovery_guarantors_member_id'), table_name='recovery_guarantors')
    op.drop_index(op.f('ix_recovery_guarantors_institution_id'), table_name='recovery_guarantors')
    op.drop_table('recovery_guarantors')
    op.drop_index(op.f('ix_recoveries_institution_id'), table_name='recoveries')
    op.drop_table('recoveries')
