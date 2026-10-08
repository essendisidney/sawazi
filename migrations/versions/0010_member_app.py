"""member app: sign-in tables, applications sent from the app, system-sent sign-in SMS

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-08 11:14:39.674560
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('member_otps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('code_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('verify_token_hash', sa.String(length=64), nullable=True),
    sa.Column('used_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('verify_token_hash')
    )
    with op.batch_alter_table('member_otps', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_member_otps_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_member_otps_phone'), ['phone'], unique=False)

    op.create_table('member_credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('pin_hash', sa.String(length=200), nullable=False),
    sa.Column('failed_attempts', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('member_id')
    )
    with op.batch_alter_table('member_credentials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_member_credentials_institution_id'), ['institution_id'], unique=False)

    op.create_table('member_devices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('member_devices', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_member_devices_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_member_devices_member_id'), ['member_id'], unique=False)

    op.create_table('member_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['member_devices.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('member_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_member_sessions_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_member_sessions_member_id'), ['member_id'], unique=False)

    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=20), nullable=False, server_default='staff'))
        batch_op.add_column(sa.Column('nominated_guarantors', sa.JSON(), nullable=True))
        batch_op.alter_column('prepared_by_user_id',
               existing_type=sa.INTEGER(),
               nullable=True)

    with op.batch_alter_table('sms_messages', schema=None) as batch_op:
        batch_op.alter_column('approved_by_user_id',
               existing_type=sa.INTEGER(),
               nullable=True)



def downgrade() -> None:
    with op.batch_alter_table('sms_messages', schema=None) as batch_op:
        batch_op.alter_column('approved_by_user_id',
               existing_type=sa.INTEGER(),
               nullable=False)

    with op.batch_alter_table('loan_applications', schema=None) as batch_op:
        batch_op.alter_column('prepared_by_user_id',
               existing_type=sa.INTEGER(),
               nullable=False)
        batch_op.drop_column('nominated_guarantors')
        batch_op.drop_column('source')

    with op.batch_alter_table('member_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_member_sessions_member_id'))
        batch_op.drop_index(batch_op.f('ix_member_sessions_institution_id'))

    op.drop_table('member_sessions')
    with op.batch_alter_table('member_devices', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_member_devices_member_id'))
        batch_op.drop_index(batch_op.f('ix_member_devices_institution_id'))

    op.drop_table('member_devices')
    with op.batch_alter_table('member_credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_member_credentials_institution_id'))

    op.drop_table('member_credentials')
    with op.batch_alter_table('member_otps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_member_otps_phone'))
        batch_op.drop_index(batch_op.f('ix_member_otps_created_at'))

    op.drop_table('member_otps')
