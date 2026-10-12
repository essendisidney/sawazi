"""sign-in protections: staff login attempts per address, member sign-in codes per address, PIN lock-outs in a row

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-12 18:19:20.303923
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('login_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('ip', sa.String(length=45), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('ok', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('login_attempts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_login_attempts_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_login_attempts_email'), ['email'], unique=False)
        batch_op.create_index(batch_op.f('ix_login_attempts_ip'), ['ip'], unique=False)

    with op.batch_alter_table('member_credentials', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lockouts', sa.Integer(), server_default='0', nullable=False))

    with op.batch_alter_table('member_otps', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ip', sa.String(length=45), nullable=True))
        batch_op.create_index(batch_op.f('ix_member_otps_ip'), ['ip'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('member_otps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_member_otps_ip'))
        batch_op.drop_column('ip')

    with op.batch_alter_table('member_credentials', schema=None) as batch_op:
        batch_op.drop_column('lockouts')

    with op.batch_alter_table('login_attempts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_login_attempts_ip'))
        batch_op.drop_index(batch_op.f('ix_login_attempts_email'))
        batch_op.drop_index(batch_op.f('ix_login_attempts_at'))

    op.drop_table('login_attempts')
