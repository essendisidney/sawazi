"""pin the audit trigger function's search_path (Supabase security advisor: function_search_path_mutable)

Revision ID: 0011
Revises: 0010
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER FUNCTION sawazi_audit_append_only() SET search_path = ''")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER FUNCTION sawazi_audit_append_only() RESET search_path")
