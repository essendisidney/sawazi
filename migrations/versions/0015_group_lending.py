"""group lending: groups with joint liability, members, meeting sheets

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-12 19:12:07.623906
"""
from alembic import op
import sqlalchemy as sa


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('lending_groups',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('group_no', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('meeting_day', sa.Integer(), nullable=True),
    sa.Column('frequency', sa.String(length=12), nullable=False),
    sa.Column('officer_user_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['officer_user_id'], ['staff_users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'group_no')
    )
    with op.batch_alter_table('lending_groups', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_lending_groups_institution_id'), ['institution_id'], unique=False)

    op.create_table('group_members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('joined_on', sa.Date(), nullable=False),
    sa.Column('left_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['group_id'], ['lending_groups.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('group_members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_group_members_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_members_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_group_members_member_id'), ['member_id'], unique=False)

    op.create_table('meeting_sheets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('group_id', sa.Integer(), nullable=False),
    sa.Column('meeting_on', sa.Date(), nullable=False),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('cash_cents', sa.BigInteger(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('submitted_at', sa.DateTime(), nullable=True),
    sa.Column('banked_at', sa.DateTime(), nullable=True),
    sa.Column('bank_transaction_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['bank_transaction_id'], ['transactions.id'], ),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['staff_users.id'], ),
    sa.ForeignKeyConstraint(['group_id'], ['lending_groups.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('group_id', 'meeting_on')
    )
    with op.batch_alter_table('meeting_sheets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_meeting_sheets_group_id'), ['group_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_meeting_sheets_institution_id'), ['institution_id'], unique=False)

    op.create_table('sheet_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('sheet_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=True),
    sa.Column('due_cents', sa.BigInteger(), nullable=False),
    sa.Column('mpesa_cents', sa.BigInteger(), nullable=False),
    sa.Column('cash_cents', sa.BigInteger(), nullable=False),
    sa.Column('present', sa.Boolean(), nullable=False),
    sa.Column('note', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.ForeignKeyConstraint(['sheet_id'], ['meeting_sheets.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sheet_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sheet_lines_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sheet_lines_sheet_id'), ['sheet_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('sheet_lines', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sheet_lines_sheet_id'))
        batch_op.drop_index(batch_op.f('ix_sheet_lines_institution_id'))

    op.drop_table('sheet_lines')
    with op.batch_alter_table('meeting_sheets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_meeting_sheets_institution_id'))
        batch_op.drop_index(batch_op.f('ix_meeting_sheets_group_id'))

    op.drop_table('meeting_sheets')
    with op.batch_alter_table('group_members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_group_members_member_id'))
        batch_op.drop_index(batch_op.f('ix_group_members_institution_id'))
        batch_op.drop_index(batch_op.f('ix_group_members_group_id'))

    op.drop_table('group_members')
    with op.batch_alter_table('lending_groups', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_lending_groups_institution_id'))

    op.drop_table('lending_groups')
