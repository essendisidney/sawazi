"""loan top-ups: an application can top up an existing loan

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-12 18:43:28.127925
"""
from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('top_up_loan_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_loan_applications_top_up_loan_id', 'loans', ['top_up_loan_id'], ['id'])



def downgrade() -> None:
    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.drop_constraint('fk_loan_applications_top_up_loan_id', type_='foreignkey')
        batch_op.drop_column('top_up_loan_id')

