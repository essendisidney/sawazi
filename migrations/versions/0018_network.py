"""guarantee network: institutions opt in

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-13 14:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('institutions', sa.Column('network_opt_in', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('institutions', 'network_opt_in')
