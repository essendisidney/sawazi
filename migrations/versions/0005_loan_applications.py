"""loan applications

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07 01:51:58.635701
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('loan_applications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('term_months', sa.Integer(), nullable=False),
    sa.Column('purpose', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('appraisal', sa.JSON(), nullable=True),
    sa.Column('appraisal_outcome', sa.String(length=20), nullable=True),
    sa.Column('approvals_needed', sa.Integer(), nullable=False),
    sa.Column('override_reason', sa.Text(), nullable=True),
    sa.Column('prepared_by_user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('submitted_at', sa.DateTime(), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('exported_at', sa.DateTime(), nullable=True),
    sa.Column('disbursed_loan_id', sa.Integer(), nullable=True),
    sa.Column('disbursed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['disbursed_loan_id'], ['loans.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.ForeignKeyConstraint(['prepared_by_user_id'], ['staff_users.id'], ),
    sa.ForeignKeyConstraint(['product_id'], ['loan_products.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_loan_applications_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_loan_applications_member_id'), ['member_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_loan_applications_status'), ['status'], unique=False)

    op.create_table('loan_decisions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('decision', sa.String(length=10), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('appraisal_outcome', sa.String(length=20), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['application_id'], ['loan_applications.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['staff_users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('application_id', 'user_id')
    )
    with op.batch_alter_table('loan_decisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_loan_decisions_application_id'), ['application_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_loan_decisions_institution_id'), ['institution_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('loan_decisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_loan_decisions_institution_id'))
        batch_op.drop_index(batch_op.f('ix_loan_decisions_application_id'))

    op.drop_table('loan_decisions')
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_loan_applications_status'))
        batch_op.drop_index(batch_op.f('ix_loan_applications_member_id'))
        batch_op.drop_index(batch_op.f('ix_loan_applications_institution_id'))

    op.drop_table('loan_applications')
