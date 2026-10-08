"""portfolio snapshots

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07 12:35:54.728794
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('portfolio_snapshots',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('as_of', sa.Date(), nullable=False),
    sa.Column('taken_at', sa.DateTime(), nullable=False),
    sa.Column('figures', sa.JSON(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'as_of')
    )
    with op.batch_alter_table('portfolio_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_portfolio_snapshots_institution_id'), ['institution_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('portfolio_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_portfolio_snapshots_institution_id'))

    op.drop_table('portfolio_snapshots')
