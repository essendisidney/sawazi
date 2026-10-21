"""branches on members and loans

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-21 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('loans', sa.Column('branch', sa.String(length=80), nullable=True))
    op.add_column('members', sa.Column('branch', sa.String(length=80), nullable=True))


def downgrade() -> None:
    op.drop_column('members', 'branch')
    op.drop_column('loans', 'branch')
