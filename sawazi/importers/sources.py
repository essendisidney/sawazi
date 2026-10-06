"""Importers for each data source a SACCO or MFI can produce.

All importers take raw CSV content, so they work with any core banking system:
every system can export CSV/Excel. Re-importing the same file is safe — rows
already seen (same source + reference) are skipped.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import CheckoffRemittance, CheckoffSchedule, ExceptionItem, Loan, Member, MpesaCallback, Transaction
from .common import (
    norm_phone,
    pick,
    read_rows,
    to_cents,
    to_date,
    to_datetime,
)


@dataclass
class ImportResult:
    created: int = 0
    skipped_duplicates: int = 0
    rejected: list[str] = field(default_factory=list)
    callbacks_confirmed: int = 0  # M-Pesa only: real-time payments now seen on the statement
    callbacks_mismatched: int = 0

    def as_dict(self):
        out = {
            "created": self.created,
            "skipped_duplicates": self.skipped_duplicates,
            "rejected": self.rejected[:50],
            "rejected_count": len(self.rejected),
        }
        if self.callbacks_confirmed or self.callbacks_mismatched:
            out |= {"callbacks_confirmed": self.callbacks_confirmed, "callbacks_mismatched": self.callbacks_mismatched}
        return out


# ---------------------------------------------------------------- core exports


def import_members(s: Session, institution_id: int, content) -> ImportResult:
    res = ImportResult()
    existing = {
        m.member_no: m
        for m in s.scalars(select(Member).where(Member.institution_id == institution_id))
    }
    for i, row in enumerate(read_rows(content), start=2):
        member_no = pick(row, "member_no", "member_number", "memberno", "no", "member_id")
        name = pick(row, "name", "member_name", "full_name", "names")
        if not member_no or not name:
            res.rejected.append(f"row {i}: missing member number or name")
            continue
        m = existing.get(member_no)
        if m is None:
            m = Member(institution_id=institution_id, member_no=member_no, name=name)
            s.add(m)
            existing[member_no] = m
            res.created += 1
        else:
            res.skipped_duplicates += 1
        m.name = name
        m.phone = norm_phone(pick(row, "phone", "mobile", "phone_number", "telephone")) or m.phone
        m.id_number = pick(row, "id_number", "id_no", "national_id") or m.id_number
        m.employer = pick(row, "employer", "employer_name", "station") or m.employer
    s.commit()
    return res


def import_loans(s: Session, institution_id: int, content) -> ImportResult:
    res = ImportResult()
    members = {
        m.member_no: m.id
        for m in s.scalars(select(Member).where(Member.institution_id == institution_id))
    }
    existing = {
        ln.loan_no: ln
        for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id))
    }
    for i, row in enumerate(read_rows(content), start=2):
        loan_no = pick(row, "loan_no", "loan_number", "loan_id", "account_no")
        member_no = pick(row, "member_no", "member_number", "memberno")
        if not loan_no or member_no not in members:
            res.rejected.append(f"row {i}: missing loan number or unknown member '{member_no}'")
            continue
        ln = existing.get(loan_no)
        if ln is None:
            ln = Loan(
                institution_id=institution_id,
                member_id=members[member_no],
                loan_no=loan_no,
                principal_cents=0,
                balance_cents=0,
                installment_cents=0,
            )
            s.add(ln)
            existing[loan_no] = ln
            res.created += 1
        else:
            res.skipped_duplicates += 1
        ln.product = pick(row, "product", "loan_product", "loan_type") or ln.product or "Normal loan"
        ln.principal_cents = to_cents(pick(row, "principal", "amount", "loan_amount")) or ln.principal_cents
        ln.balance_cents = to_cents(pick(row, "balance", "outstanding", "loan_balance"))
        ln.installment_cents = to_cents(pick(row, "installment", "instalment", "monthly_repayment", "repayment"))
        ln.arrears_cents = to_cents(pick(row, "arrears", "arrears_amount", "amount_in_arrears"))
        penalty_cols = ("penalty_arrears", "penalty_in_arrears", "penalties_due", "penalty")
        interest_cols = ("interest_arrears", "interest_in_arrears", "interest_due")
        known = any(c in row for c in penalty_cols + interest_cols)  # the export carries a breakdown
        penalty, interest = to_cents(pick(row, *penalty_cols)), to_cents(pick(row, *interest_cols))
        if known and (penalty < 0 or interest < 0 or penalty + interest > ln.arrears_cents):
            # The parts must fit inside the arrears; otherwise we cannot know what is principal.
            res.rejected.append(f"row {i}: {loan_no} penalty + interest arrears exceed total arrears; "
                                f"breakdown ignored, arrears kept as one amount")
            known, penalty, interest = False, 0, 0
        ln.penalty_arrears_cents, ln.interest_arrears_cents = penalty, interest
        ln.arrears_breakdown = known
        ln.days_in_arrears = int(float(pick(row, "days_in_arrears", "days_arrears", "dpd") or 0))
        ln.disbursed_on = to_date(pick(row, "disbursed_on", "disbursement_date", "date_disbursed"))
        ln.next_due_on = to_date(pick(row, "next_due_on", "next_due_date", "due_date"))
        via = pick(row, "repays_via", "repayment_mode", "mode").lower()
        ln.repays_via = "checkoff" if "check" in via else ("bank" if "bank" in via else "mpesa")
        ln.status = "closed" if ln.balance_cents <= 0 else "active"
    s.commit()
    return res


# ------------------------------------------------------------------ money in

_OTHER_PARTY = re.compile(r"(?P<phone>\d{9,12})?\s*-?\s*(?P<name>[A-Za-z][A-Za-z .'-]*)?")


def _existing_refs(s: Session, institution_id: int, source: str) -> set[str]:
    return set(
        s.scalars(
            select(Transaction.reference).where(
                Transaction.institution_id == institution_id, Transaction.source == source
            )
        )
    )


def import_mpesa_statement(s: Session, institution_id: int, content) -> ImportResult:
    """Safaricom paybill statement export (M-PESA org portal CSV).

    Uses: Receipt No., Completion Time, Paid In, Other Party Info, A/C No.,
    Transaction Status. Outgoing lines (Withdrawn) are ignored.
    """
    res = ImportResult()
    seen = _existing_refs(s, institution_id, "mpesa")
    pending_callbacks = _unconfirmed_callbacks(s, institution_id)
    for i, row in enumerate(read_rows(content), start=2):
        receipt = pick(row, "receipt_no", "receipt", "transaction_id", "trans_id")
        paid_in = to_cents(pick(row, "paid_in", "amount", "credit"))
        status = pick(row, "transaction_status", "status").lower()
        if not receipt or paid_in <= 0:
            continue  # outgoing / charge / blank lines
        if status and status not in {"completed", "success", "successful"}:
            continue
        other = pick(row, "other_party_info", "sender", "msisdn_name", "customer")
        phone = norm_phone(pick(row, "msisdn", "phone"))
        name = pick(row, "name", "customer_name")
        if other:
            m = _OTHER_PARTY.match(other)
            if m:
                phone = phone or norm_phone(m.group("phone"))
                name = name or (m.group("name") or "").strip()
        if receipt in pending_callbacks:
            _confirm_callbacks(s, institution_id, pending_callbacks.pop(receipt), paid_in, phone, name, res)
        if receipt in seen:
            res.skipped_duplicates += 1
            continue
        when = to_datetime(pick(row, "completion_time", "transaction_time", "date", "trans_time"))
        if when is None:
            res.rejected.append(f"row {i}: unreadable date for {receipt}")
            continue
        s.add(
            Transaction(
                institution_id=institution_id,
                source="mpesa",
                reference=receipt,
                txn_time=when,
                amount_cents=paid_in,
                payer_name=name or None,
                payer_phone=phone,
                account_ref=pick(row, "a_c_no", "ac_no", "account_no", "account", "bill_ref_number", "billrefnumber"),
                narrative=pick(row, "details"),
            )
        )
        seen.add(receipt)
        res.created += 1
    s.commit()
    return res


def _unconfirmed_callbacks(s: Session, institution_id: int) -> dict[str, list[MpesaCallback]]:
    out: dict[str, list[MpesaCallback]] = {}
    for c in s.scalars(select(MpesaCallback).where(
            MpesaCallback.institution_id == institution_id, MpesaCallback.kind == "confirmation",
            MpesaCallback.statement_confirmed_at.is_(None))):
        out.setdefault(c.trans_id, []).append(c)
    return out


def _confirm_callbacks(s: Session, institution_id: int, callbacks: list[MpesaCallback], paid_in: int,
                       phone: str | None, name: str | None, res: ImportResult) -> None:
    """The statement is the truth. Matching amounts confirm the callback and fill in payer details Safaricom
    masked in the callback; a different amount means the callback cannot be trusted."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    t = s.get(Transaction, callbacks[0].transaction_id) if callbacks[0].transaction_id else None
    for c in callbacks:
        c.statement_confirmed_at = now
    if any(c.amount_cents != paid_in for c in callbacks) or (t and t.amount_cents != paid_in):
        msg = (f"M-Pesa {callbacks[0].trans_id}: real-time callback said KES {callbacks[0].amount_cents / 100:,.2f}, "
               f"paybill statement says KES {paid_in / 100:,.2f}. Check allocations to this payment.")
        for c in callbacks:
            c.statement_mismatch = msg
        s.add(ExceptionItem(institution_id=institution_id, kind="c2b_mismatch", severity="high",
                            transaction_id=t.id if t else None, member_id=t.member_id if t else None,
                            amount_cents=paid_in, detail=msg))
        res.callbacks_mismatched += 1
        return
    if t:
        t.payer_phone = t.payer_phone or phone
        t.payer_name = t.payer_name or name or None
    res.callbacks_confirmed += 1


def import_bank_statement(s: Session, institution_id: int, content) -> ImportResult:
    """Generic bank statement CSV: Date, Narrative/Description, Reference, Credit."""
    res = ImportResult()
    seen = _existing_refs(s, institution_id, "bank")
    for i, row in enumerate(read_rows(content), start=2):
        credit = to_cents(pick(row, "credit", "credit_amount", "deposits", "money_in", "paid_in"))
        if credit <= 0:
            continue
        when = to_datetime(pick(row, "date", "value_date", "transaction_date", "posting_date"))
        if when is None:
            res.rejected.append(f"row {i}: unreadable date")
            continue
        narrative = pick(row, "narrative", "description", "details", "particulars")
        ref = pick(row, "reference", "ref", "cheque_no", "transaction_ref")
        if not ref:  # banks don't always give one: build a stable fingerprint
            ref = "H" + hashlib.sha1(f"{when.isoformat()}|{credit}|{narrative}".encode()).hexdigest()[:16]
        if ref in seen:
            res.skipped_duplicates += 1
            continue
        s.add(
            Transaction(
                institution_id=institution_id,
                source="bank",
                reference=ref,
                txn_time=when,
                amount_cents=credit,
                narrative=narrative,
                account_ref=pick(row, "account_ref", "member_no"),
            )
        )
        seen.add(ref)
        res.created += 1
    s.commit()
    return res


# ------------------------------------------------------------------ check-off


def import_checkoff_schedule(s: Session, institution_id: int, employer: str, period: str, content) -> ImportResult:
    res = ImportResult()
    members = {
        m.member_no: m.id
        for m in s.scalars(select(Member).where(Member.institution_id == institution_id))
    }
    existing = {
        c.member_id: c
        for c in s.scalars(
            select(CheckoffSchedule).where(
                CheckoffSchedule.institution_id == institution_id,
                CheckoffSchedule.employer == employer,
                CheckoffSchedule.period == period,
            )
        )
    }
    for i, row in enumerate(read_rows(content), start=2):
        member_no = pick(row, "member_no", "member_number", "memberno")
        if member_no not in members:
            res.rejected.append(f"row {i}: unknown member '{member_no}'")
            continue
        amount = to_cents(pick(row, "expected", "amount", "deduction", "total"))
        mid = members[member_no]
        if mid in existing:
            existing[mid].expected_cents = amount
            res.skipped_duplicates += 1
            continue
        c = CheckoffSchedule(
            institution_id=institution_id, employer=employer, period=period, member_id=mid, expected_cents=amount
        )
        s.add(c)
        existing[mid] = c
        res.created += 1
    s.commit()
    return res


def import_checkoff_remittance(s: Session, institution_id: int, employer: str, period: str, content) -> ImportResult:
    """Employer's remittance advice. Replaces any earlier upload for the same employer and period."""
    res = ImportResult()
    s.query(CheckoffRemittance).filter_by(
        institution_id=institution_id, employer=employer, period=period
    ).delete()
    for i, row in enumerate(read_rows(content), start=2):
        amount = to_cents(pick(row, "amount", "remitted", "deduction", "total"))
        member_no = pick(row, "member_no", "member_number", "memberno", "sacco_no")
        name = pick(row, "name", "payroll_name", "employee_name")
        if not member_no and not name:
            res.rejected.append(f"row {i}: no member number or name")
            continue
        s.add(
            CheckoffRemittance(
                institution_id=institution_id,
                employer=employer,
                period=period,
                member_no=member_no or None,
                payroll_name=name or None,
                remitted_cents=amount,
                received_on=to_date(pick(row, "received_on", "date", "payment_date")),
            )
        )
        res.created += 1
    s.commit()
    return res
