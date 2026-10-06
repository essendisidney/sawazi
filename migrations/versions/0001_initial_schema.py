"""initial schema: every table as of the end of Phase 1

Money columns are BigInteger (64-bit cents). Generated with --autogenerate and reviewed.

Revision ID: 0001
Revises: 
Create Date: 2026-10-06 15:32:52.245303
"""
from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('institutions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('paybill', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('audit_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('actor_kind', sa.String(length=20), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('actor_name', sa.String(length=200), nullable=False),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('entity_type', sa.String(length=40), nullable=True),
    sa.Column('entity_id', sa.Integer(), nullable=True),
    sa.Column('before', sa.JSON(), nullable=True),
    sa.Column('after', sa.JSON(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_events_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_institution_id'), ['institution_id'], unique=False)

    op.create_table('members',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('member_no', sa.String(length=40), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=True),
    sa.Column('id_number', sa.String(length=20), nullable=True),
    sa.Column('employer', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'member_no')
    )
    with op.batch_alter_table('members', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_members_id_number'), ['id_number'], unique=False)
        batch_op.create_index(batch_op.f('ix_members_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_members_phone'), ['phone'], unique=False)

    op.create_table('sms_opt_outs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'phone')
    )
    with op.batch_alter_table('sms_opt_outs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sms_opt_outs_institution_id'), ['institution_id'], unique=False)

    op.create_table('sms_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('service_name', sa.String(length=100), nullable=True),
    sa.Column('opt_out_text', sa.String(length=160), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id')
    )
    op.create_table('staff_users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('password_hash', sa.String(length=200), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_users_institution_id'), ['institution_id'], unique=False)

    op.create_table('allocation_rules',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('loan_order', sa.String(length=30), nullable=False),
    sa.Column('arrears_order', sa.JSON(), nullable=False),
    sa.Column('pay_current_installment', sa.Boolean(), nullable=False),
    sa.Column('excess', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['updated_by_user_id'], ['staff_users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id')
    )
    op.create_table('api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['created_by_user_id'], ['staff_users.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key_hash')
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_keys_institution_id'), ['institution_id'], unique=False)

    op.create_table('checkoff_remittance',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('employer', sa.String(length=200), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('member_no', sa.String(length=40), nullable=True),
    sa.Column('payroll_name', sa.String(length=200), nullable=True),
    sa.Column('member_id', sa.Integer(), nullable=True),
    sa.Column('remitted_cents', sa.BigInteger(), nullable=False),
    sa.Column('received_on', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('checkoff_remittance', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_checkoff_remittance_institution_id'), ['institution_id'], unique=False)

    op.create_table('checkoff_schedule',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('employer', sa.String(length=200), nullable=False),
    sa.Column('period', sa.String(length=7), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('expected_cents', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'employer', 'period', 'member_id')
    )
    with op.batch_alter_table('checkoff_schedule', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_checkoff_schedule_institution_id'), ['institution_id'], unique=False)

    op.create_table('loans',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=False),
    sa.Column('loan_no', sa.String(length=40), nullable=False),
    sa.Column('product', sa.String(length=80), nullable=False),
    sa.Column('principal_cents', sa.BigInteger(), nullable=False),
    sa.Column('balance_cents', sa.BigInteger(), nullable=False),
    sa.Column('installment_cents', sa.BigInteger(), nullable=False),
    sa.Column('arrears_cents', sa.BigInteger(), nullable=False),
    sa.Column('penalty_arrears_cents', sa.BigInteger(), nullable=False),
    sa.Column('interest_arrears_cents', sa.BigInteger(), nullable=False),
    sa.Column('arrears_breakdown', sa.Boolean(), nullable=False),
    sa.Column('days_in_arrears', sa.Integer(), nullable=False),
    sa.Column('disbursed_on', sa.Date(), nullable=True),
    sa.Column('next_due_on', sa.Date(), nullable=True),
    sa.Column('repays_via', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'loan_no')
    )
    with op.batch_alter_table('loans', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_loans_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_loans_member_id'), ['member_id'], unique=False)

    op.create_table('staff_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['staff_users.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('staff_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_sessions_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_staff_sessions_user_id'), ['user_id'], unique=False)

    op.create_table('transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('reference', sa.String(length=80), nullable=False),
    sa.Column('txn_time', sa.DateTime(), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('payer_name', sa.String(length=200), nullable=True),
    sa.Column('payer_phone', sa.String(length=20), nullable=True),
    sa.Column('account_ref', sa.String(length=80), nullable=True),
    sa.Column('narrative', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('member_id', sa.Integer(), nullable=True),
    sa.Column('match_method', sa.String(length=40), nullable=True),
    sa.Column('match_confidence', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('institution_id', 'source', 'reference')
    )
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_transactions_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_transactions_status'), ['status'], unique=False)

    op.create_table('allocations',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('transaction_id', sa.Integer(), nullable=False),
    sa.Column('target', sa.String(length=20), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=True),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('allocations', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_allocations_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_allocations_transaction_id'), ['transaction_id'], unique=False)

    op.create_table('exceptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.Column('transaction_id', sa.Integer(), nullable=True),
    sa.Column('member_id', sa.Integer(), nullable=True),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('exceptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_exceptions_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_exceptions_kind'), ['kind'], unique=False)
        batch_op.create_index(batch_op.f('ix_exceptions_status'), ['status'], unique=False)

    op.create_table('mpesa_callbacks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('trans_id', sa.String(length=40), nullable=False),
    sa.Column('amount_cents', sa.BigInteger(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('received_at', sa.DateTime(), nullable=False),
    sa.Column('ip', sa.String(length=45), nullable=True),
    sa.Column('transaction_id', sa.Integer(), nullable=True),
    sa.Column('statement_confirmed_at', sa.DateTime(), nullable=True),
    sa.Column('statement_mismatch', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('mpesa_callbacks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mpesa_callbacks_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_mpesa_callbacks_trans_id'), ['trans_id'], unique=False)

    op.create_table('reminders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('loan_id', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=20), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('priority_score', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reminders_institution_id'), ['institution_id'], unique=False)

    op.create_table('sms_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('institution_id', sa.Integer(), nullable=False),
    sa.Column('reminder_id', sa.Integer(), nullable=True),
    sa.Column('loan_id', sa.Integer(), nullable=True),
    sa.Column('member_id', sa.Integer(), nullable=True),
    sa.Column('phone', sa.String(length=20), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('provider', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('provider_message_id', sa.String(length=64), nullable=True),
    sa.Column('provider_status', sa.String(length=10), nullable=True),
    sa.Column('provider_description', sa.String(length=300), nullable=True),
    sa.Column('approved_by_user_id', sa.Integer(), nullable=False),
    sa.Column('approved_at', sa.DateTime(), nullable=False),
    sa.Column('sent_at', sa.DateTime(), nullable=True),
    sa.Column('delivery_status', sa.String(length=100), nullable=True),
    sa.Column('delivered_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['approved_by_user_id'], ['staff_users.id'], ),
    sa.ForeignKeyConstraint(['institution_id'], ['institutions.id'], ),
    sa.ForeignKeyConstraint(['loan_id'], ['loans.id'], ),
    sa.ForeignKeyConstraint(['member_id'], ['members.id'], ),
    sa.ForeignKeyConstraint(['reminder_id'], ['reminders.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sms_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sms_messages_institution_id'), ['institution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sms_messages_loan_id'), ['loan_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sms_messages_phone'), ['phone'], unique=False)
        batch_op.create_index(batch_op.f('ix_sms_messages_provider_message_id'), ['provider_message_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sms_messages_reminder_id'), ['reminder_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_sms_messages_status'), ['status'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('sms_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sms_messages_status'))
        batch_op.drop_index(batch_op.f('ix_sms_messages_reminder_id'))
        batch_op.drop_index(batch_op.f('ix_sms_messages_provider_message_id'))
        batch_op.drop_index(batch_op.f('ix_sms_messages_phone'))
        batch_op.drop_index(batch_op.f('ix_sms_messages_loan_id'))
        batch_op.drop_index(batch_op.f('ix_sms_messages_institution_id'))

    op.drop_table('sms_messages')
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reminders_institution_id'))

    op.drop_table('reminders')
    with op.batch_alter_table('mpesa_callbacks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mpesa_callbacks_trans_id'))
        batch_op.drop_index(batch_op.f('ix_mpesa_callbacks_institution_id'))

    op.drop_table('mpesa_callbacks')
    with op.batch_alter_table('exceptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_exceptions_status'))
        batch_op.drop_index(batch_op.f('ix_exceptions_kind'))
        batch_op.drop_index(batch_op.f('ix_exceptions_institution_id'))

    op.drop_table('exceptions')
    with op.batch_alter_table('allocations', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_allocations_transaction_id'))
        batch_op.drop_index(batch_op.f('ix_allocations_institution_id'))

    op.drop_table('allocations')
    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_transactions_status'))
        batch_op.drop_index(batch_op.f('ix_transactions_institution_id'))

    op.drop_table('transactions')
    with op.batch_alter_table('staff_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_staff_sessions_user_id'))
        batch_op.drop_index(batch_op.f('ix_staff_sessions_institution_id'))

    op.drop_table('staff_sessions')
    with op.batch_alter_table('loans', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_loans_member_id'))
        batch_op.drop_index(batch_op.f('ix_loans_institution_id'))

    op.drop_table('loans')
    with op.batch_alter_table('checkoff_schedule', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_checkoff_schedule_institution_id'))

    op.drop_table('checkoff_schedule')
    with op.batch_alter_table('checkoff_remittance', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_checkoff_remittance_institution_id'))

    op.drop_table('checkoff_remittance')
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_keys_institution_id'))

    op.drop_table('api_keys')
    op.drop_table('allocation_rules')
    with op.batch_alter_table('staff_users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_staff_users_institution_id'))

    op.drop_table('staff_users')
    op.drop_table('sms_settings')
    with op.batch_alter_table('sms_opt_outs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sms_opt_outs_institution_id'))

    op.drop_table('sms_opt_outs')
    with op.batch_alter_table('members', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_members_phone'))
        batch_op.drop_index(batch_op.f('ix_members_institution_id'))
        batch_op.drop_index(batch_op.f('ix_members_id_number'))

    op.drop_table('members')
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_events_institution_id'))
        batch_op.drop_index(batch_op.f('ix_audit_events_at'))
        batch_op.drop_index(batch_op.f('ix_audit_events_action'))

    op.drop_table('audit_events')
    op.drop_table('institutions')
