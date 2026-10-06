"""audit log is append-only in the database itself

The app already refuses to change audit events. This makes the database refuse too, so a direct
SQL UPDATE or DELETE (a script, a console session, a compromised app user) cannot rewrite history.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

MESSAGE = "audit events are append-only"


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"""
            CREATE FUNCTION sawazi_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION '{MESSAGE}';
            END $$;
        """)
        op.execute("""
            CREATE TRIGGER audit_events_append_only
            BEFORE UPDATE OR DELETE OR TRUNCATE ON audit_events
            FOR EACH STATEMENT EXECUTE FUNCTION sawazi_audit_append_only();
        """)
    else:  # SQLite (local development)
        for event in ("UPDATE", "DELETE"):
            op.execute(f"""
                CREATE TRIGGER audit_events_no_{event.lower()} BEFORE {event} ON audit_events
                BEGIN SELECT RAISE(ABORT, '{MESSAGE}'); END;
            """)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER audit_events_append_only ON audit_events")
        op.execute("DROP FUNCTION sawazi_audit_append_only()")
    else:
        for event in ("update", "delete"):
            op.execute(f"DROP TRIGGER audit_events_no_{event}")
