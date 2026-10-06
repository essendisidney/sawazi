"""Core data model.

Every table carries institution_id, so one deployment serves many SACCOs and
microfinance institutions (multi-tenant). Money is stored as integer cents to
keep reconciliation exact, in 64-bit columns (BigInteger): a 32-bit integer
would cap amounts at about KES 21.5 million on PostgreSQL.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Institution(Base):
    __tablename__ = "institutions"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20), default="sacco")  # sacco | mfi
    paybill: Mapped[str | None] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Member(Base):
    __tablename__ = "members"
    __table_args__ = (UniqueConstraint("institution_id", "member_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    member_no: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(20), index=True)
    id_number: Mapped[str | None] = mapped_column(String(20), index=True)
    employer: Mapped[str | None] = mapped_column(String(200))
    # From the core system / payroll. None means "not supplied": never treat unknown as zero.
    joined_on: Mapped[date | None] = mapped_column(Date)
    deposits_cents: Mapped[int | None] = mapped_column(BigInteger)
    shares_cents: Mapped[int | None] = mapped_column(BigInteger)
    balances_as_of: Mapped[date | None] = mapped_column(Date)
    gross_pay_cents: Mapped[int | None] = mapped_column(BigInteger)  # monthly, from payslip / payroll
    net_pay_cents: Mapped[int | None] = mapped_column(BigInteger)  # monthly take-home after all deductions
    pay_as_of: Mapped[date | None] = mapped_column(Date)

    loans: Mapped[list[Loan]] = relationship(back_populates="member")


class Loan(Base):
    __tablename__ = "loans"
    __table_args__ = (UniqueConstraint("institution_id", "loan_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    loan_no: Mapped[str] = mapped_column(String(40))
    product: Mapped[str] = mapped_column(String(80), default="Normal loan")
    principal_cents: Mapped[int] = mapped_column(BigInteger)
    balance_cents: Mapped[int] = mapped_column(BigInteger)
    installment_cents: Mapped[int] = mapped_column(BigInteger)
    arrears_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    # Parts of arrears_cents, as reported by the core system (Sawazi never computes them). The rest is principal.
    penalty_arrears_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    interest_arrears_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    arrears_breakdown: Mapped[bool] = mapped_column(Boolean, default=False)  # the export gave the parts
    days_in_arrears: Mapped[int] = mapped_column(Integer, default=0)
    disbursed_on: Mapped[date | None] = mapped_column(Date)
    next_due_on: Mapped[date | None] = mapped_column(Date)
    repays_via: Mapped[str] = mapped_column(String(20), default="mpesa")  # mpesa | checkoff | bank
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | closed | written_off

    member: Mapped[Member] = relationship(back_populates="loans")


class Transaction(Base):
    """One money-in line from an M-Pesa, bank or check-off source."""

    __tablename__ = "transactions"
    # Idempotency: the same receipt can't be imported twice for an institution.
    __table_args__ = (UniqueConstraint("institution_id", "source", "reference"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    source: Mapped[str] = mapped_column(String(20))  # mpesa | bank | checkoff
    reference: Mapped[str] = mapped_column(String(80))
    txn_time: Mapped[datetime] = mapped_column(DateTime)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    payer_name: Mapped[str | None] = mapped_column(String(200))
    payer_phone: Mapped[str | None] = mapped_column(String(20))
    account_ref: Mapped[str | None] = mapped_column(String(80))
    narrative: Mapped[str | None] = mapped_column(Text)

    status: Mapped[str] = mapped_column(String(20), default="unmatched", index=True)
    # unmatched | allocated | suspense
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    match_method: Mapped[str | None] = mapped_column(String(40))
    match_confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100

    allocations: Mapped[list[Allocation]] = relationship(back_populates="transaction")


class Allocation(Base):
    """How a matched payment was split across loans, deposits and shares."""

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), index=True)
    # loan_penalty | loan_interest | loan_principal | loan_arrears (no breakdown) | loan_installment | deposits | shares
    target: Mapped[str] = mapped_column(String(20))
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger)

    transaction: Mapped[Transaction] = relationship(back_populates="allocations")


class CheckoffSchedule(Base):
    """What the SACCO asked an employer to deduct from each member for a period."""

    __tablename__ = "checkoff_schedule"
    __table_args__ = (UniqueConstraint("institution_id", "employer", "period", "member_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    employer: Mapped[str] = mapped_column(String(200))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"))
    expected_cents: Mapped[int] = mapped_column(BigInteger)


class CheckoffRemittance(Base):
    """What the employer actually remitted, line by line."""

    __tablename__ = "checkoff_remittance"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    employer: Mapped[str] = mapped_column(String(200))
    period: Mapped[str] = mapped_column(String(7))
    member_no: Mapped[str | None] = mapped_column(String(40))
    payroll_name: Mapped[str | None] = mapped_column(String(200))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    remitted_cents: Mapped[int] = mapped_column(BigInteger)
    received_on: Mapped[date | None] = mapped_column(Date)


class ExceptionItem(Base):
    """Anything a person needs to look at: suspense items, short remittances, anomalies."""

    __tablename__ = "exceptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(10), default="medium")  # low | medium | high
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    detail: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open | resolved
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Reminder(Base):
    """A collections action queued for a loan."""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    loan_id: Mapped[int] = mapped_column(ForeignKey("loans.id"))
    channel: Mapped[str] = mapped_column(String(20))  # sms | call | field_visit | guarantor_notice
    message: Mapped[str] = mapped_column(Text)
    priority_score: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | sent | done
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class StaffUser(Base):
    """A person at one institution who uses Sawazi. Email is unique platform-wide: one login, one institution."""

    __tablename__ = "staff_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))  # admin | accountant | credit_officer | viewer
    password_hash: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)


class StaffSession(Base):
    """A login. Only the SHA-256 of the bearer token is stored."""

    __tablename__ = "staff_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("staff_users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class ApiKey(Base):
    """Machine access for one institution (core banking sync, scheduled uploads).
    Belongs to the institution, not to the staff member who created it. Only the SHA-256 is stored."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))  # accountant | credit_officer | viewer (never admin)
    prefix: Mapped[str] = mapped_column(String(16))  # first characters, shown so staff can tell keys apart
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("staff_users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditEvent(Base):
    """Who did what, when, with the state before and after. Append-only: the ORM refuses updates
    and deletes (see sawazi/audit.py). Never holds passwords, password hashes or API keys."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime, index=True)
    actor_kind: Mapped[str] = mapped_column(String(20))  # user | api_key | platform | anonymous | provider
    actor_id: Mapped[int | None] = mapped_column(Integer)
    actor_name: Mapped[str] = mapped_column(String(200))  # snapshot, so renames don't rewrite history
    action: Mapped[str] = mapped_column(String(60), index=True)  # e.g. suspense.clear, user.update
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))


class SmsSettings(Base):
    """Per-institution SMS setup. Real sends need `enabled`; the simulate provider ignores it."""

    __tablename__ = "sms_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    service_name: Mapped[str | None] = mapped_column(String(100))  # Taifa Mobile service / sender ID
    opt_out_text: Mapped[str | None] = mapped_column(String(160))  # appended to every message, e.g. how to stop


class SmsMessage(Base):
    """One SMS to one member: who approved it, what was sent, and what the network said."""

    __tablename__ = "sms_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    reminder_id: Mapped[int | None] = mapped_column(ForeignKey("reminders.id"), index=True)
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"), index=True)
    member_id: Mapped[int | None] = mapped_column(ForeignKey("members.id"))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    body: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(20))  # simulate | taifa
    # pending -> sent | failed | unknown | simulated -> delivered | undelivered
    status: Mapped[str] = mapped_column(String(20), index=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(64), index=True)
    provider_status: Mapped[str | None] = mapped_column(String(10))
    provider_description: Mapped[str | None] = mapped_column(String(300))
    approved_by_user_id: Mapped[int] = mapped_column(ForeignKey("staff_users.id"))
    approved_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivery_status: Mapped[str | None] = mapped_column(String(100))  # raw delivery report status
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)


class SmsOptOut(Base):
    """A phone number that must not get SMS from this institution."""

    __tablename__ = "sms_opt_outs"
    __table_args__ = (UniqueConstraint("institution_id", "phone"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(20))  # member_sms | subscription | sender_blocked | staff
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class MpesaCallback(Base):
    """Every Daraja C2B validation/confirmation exactly as received. A confirmation becomes a Transaction;
    the paybill statement later confirms it (Safaricom does not sign callbacks, the statement is the truth)."""

    __tablename__ = "mpesa_callbacks"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # validation | confirmation
    trans_id: Mapped[str] = mapped_column(String(40), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime)
    ip: Mapped[str | None] = mapped_column(String(45))
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"))
    statement_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    statement_mismatch: Mapped[str | None] = mapped_column(Text)


class AllocationRules(Base):
    """How this institution wants payments split. No row means the defaults (see engine/allocation.py)."""

    __tablename__ = "allocation_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), unique=True)
    loan_order: Mapped[str] = mapped_column(String(30))
    arrears_order: Mapped[list] = mapped_column(JSON)  # e.g. ["penalty", "interest", "principal"]
    pay_current_installment: Mapped[bool] = mapped_column(Boolean)
    excess: Mapped[list] = mapped_column(JSON)  # [{"target", "percent", "max_cents"}], last takes the rest
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("staff_users.id"))


class LoanProduct(Base):
    """A loan product and the appraisal rules that go with it. Sawazi only appraises; the core system lends."""

    __tablename__ = "loan_products"
    __table_args__ = (UniqueConstraint("institution_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    code: Mapped[str] = mapped_column(String(20))  # as in the core system, e.g. DEV, EMG
    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    min_amount_cents: Mapped[int] = mapped_column(BigInteger)
    max_amount_cents: Mapped[int] = mapped_column(BigInteger)
    max_term_months: Mapped[int] = mapped_column(Integer)
    # Only to estimate the instalment for affordability. The real schedule is the core system's.
    interest_rate_bps: Mapped[int] = mapped_column(Integer)  # yearly, basis points: 1200 = 12% a year
    interest_method: Mapped[str] = mapped_column(String(10))  # reducing | flat
    deposits_multiplier_pct: Mapped[int] = mapped_column(Integer)  # 300 = 3x deposits; 0 = not checked
    min_membership_months: Mapped[int] = mapped_column(Integer)
    max_arrears_days: Mapped[int] = mapped_column(Integer)  # existing loans further behind block the application
    one_third_rule: Mapped[bool] = mapped_column(Boolean)
    guarantor_cover: Mapped[str] = mapped_column(String(20))  # above_deposits | full | none
    min_guarantors: Mapped[int] = mapped_column(Integer)
    second_approval_above_cents: Mapped[int | None] = mapped_column(BigInteger)  # two approvers above this
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
