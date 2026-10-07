"""guarantees

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07 02:07:14.859506
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('guarantees',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('guarantor_member_id', sa.Integer(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('pin_hash', sa.String(length=64), nullable=True),
    sa.Column('pin_expires_at', sa.DateTime(), nullable=True),
    sa.Column('pin_attempts', sa.Integer(), nullable=False),
    sa.Column('pins_sent', sa.Integer(), nullable=False),
    sa.Column('requested_by_user_id', sa.Integer(), nullable=False),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.Column('responded_at', sa.DateTime(), nullable=True),
    sa.Column('response_ip', sa.String(length=45), nullable=True),
    sa.ForeignKeyConstraint(['application_id'], ['loan_applications.id'], ),
    sa.ForeignKeyConstraint(['guarantor_member_id'], ['members.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['requested_by_user_id'], ['staff_users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('guarantees', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_guarantees_application_id'), ['application_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_guarantees_guarantor_member_id'), ['guarantor_member_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_guarantees_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_guarantees_status'), ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('guarantees', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_guarantees_status'))
        batch_op.drop_index(batch_op.f('ix_guarantees_institution_id'))
        batch_op.drop_index(batch_op.f('ix_guarantees_guarantor_member_id'))
        batch_op.drop_index(batch_op.f('ix_guarantees_application_id'))

    op.drop_table('guarantees')
