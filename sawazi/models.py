"""Core data model.

Every table carries institution_id, so one deployment serves many SACCOs and
microfinance institutions (multi-tenant). Money is stored as integer cents to
keep reconciliation exact.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
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

    loans: Mapped[list[Loan]] = relationship(back_populates="member")


class Loan(Base):
    __tablename__ = "loans"
    __table_args__ = (UniqueConstraint("institution_id", "loan_no"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institutions.id"), index=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id"), index=True)
    loan_no: Mapped[str] = mapped_column(String(40))
    product: Mapped[str] = mapped_column(String(80), default="Normal loan")
    principal_cents: Mapped[int] = mapped_column(Integer)
    balance_cents: Mapped[int] = mapped_column(Integer)
    installment_cents: Mapped[int] = mapped_column(Integer)
    arrears_cents: Mapped[int] = mapped_column(Integer, default=0)
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
    amount_cents: Mapped[int] = mapped_column(Integer)
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
    target: Mapped[str] = mapped_column(String(20))  # loan_arrears | loan_installment | deposits
    loan_id: Mapped[int | None] = mapped_column(ForeignKey("loans.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)

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
    expected_cents: Mapped[int] = mapped_column(Integer)


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
    remitted_cents: Mapped[int] = mapped_column(Integer)
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
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
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
