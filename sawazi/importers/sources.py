"""Importers for each data source a SACCO or MFI can produce.

All importers take raw CSV content, so they work with any core banking system:
every system can export CSV/Excel. Re-importing the same file is safe — rows
already seen (same source + reference) are skipped.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (CheckoffRemittance, CheckoffSchedule, CoreGuarantee, ExceptionItem, Loan, LoanApplication,
                      Member,
                      MpesaCallback, Transaction)
from .common import (
    BadFile,
    BadValue,
    amount_problem,
    has_column,
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
    updated: int = 0  # member balances: existing members whose figures changed
    released: int = 0  # core guarantees no longer in a complete (replace) file
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
        if self.updated:
            out["updated"] = self.updated
        if self.released:
            out["released"] = self.released
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
        too_long = _too_long(row, member_no=(member_no, 40), name=(name, 200),
                             id_number=(pick(row, "id_number", "id_no", "national_id"), 20),
                             employer=(pick(row, "employer", "employer_name", "station"), 200))
        figures = [amount_problem(row, *names) for names in _FIGURES.values()]
        if too_long or any(figures):
            res.rejected.append(f"row {i}: {member_no} {too_long or next(f for f in figures if f)}")
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
        _apply_member_figures(m, row, i, res)
    s.commit()
    return res


# Optional member figures. A blank cell means "not supplied": it never overwrites a known figure.
_FIGURES = {
    "deposits_cents": ("deposits", "deposit_balance", "total_deposits", "bosa_deposits", "savings"),
    "shares_cents": ("share_capital", "shares", "share_balance"),
    "gross_pay_cents": ("gross_pay", "gross_salary", "gross"),
    "net_pay_cents": ("net_pay", "net_salary", "take_home", "net"),
    "last_dividend_cents": ("last_dividend", "dividend", "dividends", "dividend_paid", "last_dividend_paid"),
}
_JOINED = ("date_joined", "joined_on", "joined", "registration_date", "date_registered", "membership_date")
_BAL_AS_OF = ("balances_as_at", "balance_date", "as_at", "as_of")
_PAY_AS_OF = ("pay_date", "payroll_date", "pay_period_end")


def _insider_kind(raw: str):
    """board | supervisory | staff | related; None for "none"; False when unreadable."""
    if raw in ("none", "no", "n", "-", "0"):
        return None
    for kind, words in (("supervisory", ("supervis",)), ("board", ("board", "director", "bod")),
                        ("related", ("related", "relative", "family")), ("staff", ("staff", "employee"))):
        if any(w in raw for w in words):
            return kind
    return False


def _apply_member_figures(m: Member, row: dict, i: int, res: ImportResult) -> bool:
    """Set the figures this row actually gives. A blank cell (or a missing column) changes nothing.
    Returns True if anything was set."""
    set_balance = set_pay = touched = False
    for field, names in _FIGURES.items():
        raw = pick(row, *names)
        if not raw:
            continue
        cents = to_cents(raw)
        if cents < 0:
            res.rejected.append(f"row {i}: {m.member_no} negative {field.removesuffix('_cents').replace('_', ' ')} ignored")
            continue
        setattr(m, field, cents)
        touched = True
        set_balance |= field in ("deposits_cents", "shares_cents")
        set_pay |= field in ("gross_pay_cents", "net_pay_cents")
    raw = pick(row, "branch", "branch_name", "station").strip()
    if raw:
        m.branch, touched = raw[:80], True
    raw = pick(row, "insider", "insider_type", "insider_category").strip().lower()
    if raw:
        kind = _insider_kind(raw)
        if kind is False:
            res.rejected.append(f"row {i}: {m.member_no} insider '{raw}' should be board, supervisory, staff, related "
                                f"or none")
        else:
            m.insider, touched = kind, True
    raw = pick(row, *_JOINED)
    if raw:
        joined = to_date(raw)
        if joined is None:
            res.rejected.append(f"row {i}: {m.member_no} unreadable join date '{raw}'")
        else:
            m.joined_on, touched = joined, True
    if m.gross_pay_cents is not None and m.net_pay_cents is not None and m.net_pay_cents > m.gross_pay_cents:
        res.rejected.append(f"row {i}: {m.member_no} net pay is more than gross pay; pay figures cleared")
        m.gross_pay_cents = m.net_pay_cents = None
        set_pay = False
    as_of = to_date(pick(row, *_BAL_AS_OF)) or date.today()
    if set_balance:
        m.balances_as_of = as_of
    if set_pay:
        m.pay_as_of = to_date(pick(row, *_PAY_AS_OF)) or as_of
    return touched


def _too_long(row: dict, **fields: tuple[str, int]) -> str | None:
    """A value longer than its column holds is refused with a reason (PostgreSQL would otherwise fail the upload)."""
    for label, (value, limit) in fields.items():
        if value and len(value) > limit:
            return f"{label.replace('_', ' ')} is longer than {limit} characters"
    return None


def _link_disbursed(s: Session, institution_id: int, new_loans: list[Loan]) -> None:
    """A loan Sawazi handed to the core system comes back in the loans export once disbursed. Link it only when
    there is exactly one exported application for that member and amount: never guess between two."""
    if not new_loans:
        return
    waiting = list(s.scalars(select(LoanApplication).where(LoanApplication.institution_id == institution_id,
                                                           LoanApplication.status == "exported")))
    for ln in new_loans:
        same = [a for a in waiting if a.member_id == ln.member_id and a.amount_cents == ln.principal_cents]
        if len(same) == 1:
            a = same[0]
            a.status, a.disbursed_loan_id = "disbursed", ln.id
            a.disbursed_at = datetime.now(timezone.utc).replace(tzinfo=None)
            waiting.remove(a)


def import_core_guarantees(s: Session, institution_id: int, content, replace: bool = False) -> ImportResult:
    """Guarantees from the core system: Loan No, Guarantor (member no), Amount. Re-uploading updates amounts.
    With replace=True the file is taken as the complete current list, and core guarantees missing from it are
    released. Without it, nothing is released (a partial file must never wipe exposure)."""
    res = ImportResult()
    loans = {ln.loan_no: ln for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id))}
    members = {m.member_no: m for m in s.scalars(select(Member).where(Member.institution_id == institution_id))}
    existing = {(g.loan_id, g.guarantor_member_id): g for g in s.scalars(
        select(CoreGuarantee).where(CoreGuarantee.institution_id == institution_id))}
    seen: set[tuple[int, int]] = set()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for i, row in enumerate(read_rows(content), start=2):
        loan_no = pick(row, "loan_no", "loan_number", "loan_account", "account_no")
        g_no = pick(row, "guarantor_member_no", "guarantor_no", "guarantor", "guarantor_member_number", "member_no")
        amount = to_cents(pick(row, "amount_guaranteed", "guaranteed_amount", "amount", "guarantee_amount"))
        ln, gm = loans.get(loan_no), members.get(g_no)
        if ln is None or gm is None:
            what = f"loan '{loan_no}'" if ln is None else f"guarantor '{g_no}'"
            res.rejected.append(f"row {i}: unknown {what}")
            continue
        if gm.id == ln.member_id:
            res.rejected.append(f"row {i}: {g_no} cannot guarantee their own loan {loan_no}")
            continue
        if amount <= 0:
            res.rejected.append(f"row {i}: {loan_no}/{g_no} has no guaranteed amount")
            continue
        key = (ln.id, gm.id)
        if key in seen:
            res.rejected.append(f"row {i}: {loan_no}/{g_no} appears twice; first row kept")
            continue
        seen.add(key)
        status = "released" if ln.status == "closed" else "active"
        g = existing.get(key)
        if g is None:
            g = CoreGuarantee(institution_id=institution_id, loan_id=ln.id, guarantor_member_id=gm.id,
                              amount_cents=amount, status=status, imported_at=now,
                              released_at=now if status == "released" else None)
            s.add(g)
            res.created += 1
        else:
            if (g.amount_cents, g.status) != (amount, status):
                res.updated += 1
            g.amount_cents, g.status, g.imported_at = amount, status, now
            g.released_at = now if status == "released" else None
    if replace:
        for key, g in existing.items():
            if key not in seen and g.status == "active":
                g.status, g.released_at = "released", now
                res.released += 1
    s.commit()
    return res


def import_member_balances(s: Session, institution_id: int, content) -> ImportResult:
    """Monthly balances (deposits, shares) or payroll figures (gross, net pay) for existing members.
    Never creates members; unknown member numbers are listed."""
    res = ImportResult()
    members = {m.member_no: m for m in s.scalars(select(Member).where(Member.institution_id == institution_id))}
    for i, row in enumerate(read_rows(content), start=2):
        member_no = pick(row, "member_no", "member_number", "memberno", "no", "member_id")
        m = members.get(member_no)
        if m is None:
            res.rejected.append(f"row {i}: unknown member '{member_no}'")
            continue
        bad = next((p for p in (amount_problem(row, *names) for names in _FIGURES.values()) if p), None)
        if bad:
            res.rejected.append(f"row {i}: {member_no} {bad}")
            continue
        if _apply_member_figures(m, row, i, res):
            res.updated += 1
    s.commit()
    return res


_LOAN_BALANCE = ("balance", "outstanding", "loan_balance", "outstanding_balance", "balance_outstanding", "balance_kes", "loan_balance_kes", "total_balance")
_LOAN_ARREARS = ("arrears", "arrears_amount", "amount_in_arrears", "arrears_kes", "total_arrears")


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
    new_loans: list[Loan] = []
    rows = read_rows(content)
    if rows and not has_column(rows, *_LOAN_BALANCE):
        raise BadFile("the loans file has no balance column (expected 'Balance' or 'Outstanding Balance'). Without it "
                      "every loan would look repaid, so nothing was imported.")
    if rows and not has_column(rows, *_LOAN_ARREARS):
        raise BadFile("the loans file has no arrears column (expected 'Arrears'). Without it no loan would look "
                      "overdue, so nothing was imported.")
    for i, row in enumerate(rows, start=2):
        loan_no = pick(row, "loan_no", "loan_number", "loan_id", "account_no")
        member_no = pick(row, "member_no", "member_number", "memberno")
        if not loan_no or member_no not in members:
            res.rejected.append(f"row {i}: missing loan number or unknown member '{member_no}'")
            continue
        balance_raw = pick(row, *_LOAN_BALANCE)
        problem = ("blank balance" if not balance_raw else
                   amount_problem(row, *_LOAN_BALANCE) or amount_problem(row, *_LOAN_ARREARS)
                   or amount_problem(row, "installment", "instalment", "monthly_repayment", "repayment")
                   or amount_problem(row, "principal", "amount", "loan_amount")
                   or amount_problem(row, "penalty_arrears", "penalty_in_arrears", "penalties_due", "penalty")
                   or amount_problem(row, "interest_arrears", "interest_in_arrears", "interest_due"))
        days_raw = pick(row, "days_in_arrears", "days_arrears", "dpd").replace(",", "")
        if not problem and days_raw and not re.fullmatch(r"\d+(\.0+)?", days_raw):
            problem = f"days in arrears '{days_raw}' is not a whole number"
        if problem:  # the loan keeps its last known figures; it is never closed on a figure we could not read
            res.rejected.append(f"row {i}: {loan_no} {problem}; loan left as it was")
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
            new_loans.append(ln)
            res.created += 1
        else:
            res.skipped_duplicates += 1
        ln.product = pick(row, "product", "loan_product", "loan_type") or ln.product or "Normal loan"
        ln.principal_cents = to_cents(pick(row, "principal", "amount", "loan_amount")) or ln.principal_cents
        ln.balance_cents = to_cents(balance_raw)
        ln.installment_cents = to_cents(pick(row, "installment", "instalment", "monthly_repayment", "repayment"))
        ln.arrears_cents = to_cents(pick(row, *_LOAN_ARREARS))
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
        ln.days_in_arrears = int(float(days_raw or 0))
        ln.disbursed_on = to_date(pick(row, "disbursed_on", "disbursement_date", "date_disbursed"))
        ln.next_due_on = to_date(pick(row, "next_due_on", "next_due_date", "due_date"))
        ln.officer = (pick(row, "loan_officer", "officer", "credit_officer", "relationship_officer").strip()[:120]
                      or ln.officer)
        ln.branch = pick(row, "branch", "branch_name", "station").strip()[:80] or ln.branch
        via = pick(row, "repays_via", "repayment_mode", "mode").lower()
        ln.repays_via = "checkoff" if "check" in via else ("bank" if "bank" in via else "mpesa")
        core_status = pick(row, "status", "loan_status").lower()
        if "write" in core_status or "written" in core_status:  # written off: guarantors stay liable
            ln.status = "written_off"
        else:
            ln.status = "closed" if ln.balance_cents <= 0 else "active"
    s.flush()
    _link_disbursed(s, institution_id, new_loans)
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
        problem = amount_problem(row, "paid_in", "amount", "credit")
        if problem:
            res.rejected.append(f"row {i}: {receipt or 'no receipt'} {problem}")
            continue
        if len(receipt) > 80:
            res.rejected.append(f"row {i}: receipt number longer than 80 characters")
            continue
        paid_in = to_cents(pick(row, "paid_in", "amount", "credit"))
        status = pick(row, "transaction_status", "status").lower()
        if receipt and paid_in <= 0 and "revers" in pick(row, "details").lower():
            _flag_reversal(s, institution_id, receipt, pick(row, "details"), res)
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
        txn = Transaction(
            institution_id=institution_id,
            source="mpesa",
            reference=receipt,
            txn_time=when,
            amount_cents=paid_in,
            payer_name=(name or "")[:200] or None,
            payer_phone=phone,
            account_ref=pick(row, "a_c_no", "ac_no", "account_no", "account", "bill_ref_number", "billrefnumber")[:80],
            narrative=pick(row, "details"),
        )
        if not _insert_once(s, txn):  # a live callback saved the same receipt a moment ago
            res.skipped_duplicates += 1
            continue
        seen.add(receipt)
        res.created += 1
    s.commit()
    return res


def _flag_reversal(s: Session, institution_id: int, receipt: str, details: str, res: ImportResult) -> None:
    """Safaricom reversed a paybill payment. If Sawazi already holds the original, its allocation must be undone in
    the core system: raise it once, as high priority, for a person."""
    for original in re.findall(r"\b[A-Z0-9]{10}\b", details.upper()):
        t = s.scalar(select(Transaction).where(Transaction.institution_id == institution_id,
                                               Transaction.source == "mpesa", Transaction.reference == original))
        if t is None:
            continue
        note = f"reversal {receipt}"
        if s.scalar(select(ExceptionItem.id).where(ExceptionItem.institution_id == institution_id,
                                                   ExceptionItem.kind == "mpesa_reversal",
                                                   ExceptionItem.detail.contains(note))):
            return  # already raised by an earlier upload
        s.add(ExceptionItem(institution_id=institution_id, kind="mpesa_reversal", severity="high", transaction_id=t.id,
                            member_id=t.member_id, amount_cents=t.amount_cents,
                            detail=f"M-Pesa {original} (KES {t.amount_cents / 100:,.2f}) was reversed by Safaricom "
                                   f"({note}). Undo its allocation in the core system before the next postings file."))
        res.rejected.append(f"{receipt}: reversal of {original} raised for a person to undo")
        return


def _insert_once(s: Session, txn: Transaction) -> bool:
    """Add a payment unless an identical reference was saved meanwhile (e.g. by a live M-Pesa callback):
    the rest of the file still imports instead of the whole upload failing."""
    from sqlalchemy.exc import IntegrityError

    try:
        with s.begin_nested():
            s.add(txn)
            s.flush()
        return True
    except IntegrityError:
        return False


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
    occurrence: dict[str, int] = {}  # identical lines in one statement are separate payments
    given_seen: dict[str, int] = {}
    stored = {r: (when, amount) for r, when, amount in s.execute(select(
        Transaction.reference, Transaction.txn_time, Transaction.amount_cents).where(
        Transaction.institution_id == institution_id, Transaction.source == "bank"))}
    for i, row in enumerate(read_rows(content), start=2):
        problem = amount_problem(row, "credit", "credit_amount", "deposits", "money_in", "paid_in")
        if problem:
            res.rejected.append(f"row {i}: {problem}")
            continue
        credit = to_cents(pick(row, "credit", "credit_amount", "deposits", "money_in", "paid_in"))
        if credit <= 0:
            continue
        when = to_datetime(pick(row, "date", "value_date", "transaction_date", "posting_date"))
        if when is None:
            res.rejected.append(f"row {i}: unreadable date")
            continue
        narrative = pick(row, "narrative", "description", "details", "particulars")
        given = pick(row, "reference", "ref", "cheque_no", "transaction_ref")[:80]
        # Keep the bank's own reference when it identifies this line. Banks don't always give one, and some reuse
        # one (batch or cheque numbers), so a line whose reference repeats in this statement, or was used before
        # for a different date or amount, gets a fingerprint of the whole line plus how many identical lines came
        # before it here. Two equal cash deposits on one day are two payments; re-uploading still adds nothing.
        key = f"{given}|{when.isoformat()}|{credit}|{narrative}"
        occurrence[key] = occurrence.get(key, 0) + 1
        given_seen[given] = given_seen.get(given, 0) + 1
        earlier = stored.get(given)
        if given and given_seen[given] == 1 and (earlier is None or earlier == (when, credit)):
            ref = given
        else:
            ref = "H" + hashlib.sha1(f"{key}|{occurrence[key]}".encode()).hexdigest()[:16]
        if ref in seen:
            res.skipped_duplicates += 1
            continue
        txn = Transaction(
            institution_id=institution_id,
            source="bank",
            reference=ref,
            txn_time=when,
            amount_cents=credit,
            narrative=narrative,
            account_ref=pick(row, "account_ref", "member_no")[:80],
        )
        if not _insert_once(s, txn):
            res.skipped_duplicates += 1
            continue
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
        problem = amount_problem(row, "expected", "amount", "deduction", "total")
        if problem:
            res.rejected.append(f"row {i}: {problem}")
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
        problem = amount_problem(row, "amount", "remitted", "deduction", "total")
        if problem:
            res.rejected.append(f"row {i}: {problem}")
            continue
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


# ------------------------------------------------------------------ MFI groups

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def import_groups(s: Session, institution_id: int, content) -> ImportResult:
    """Groups: Group No, Group Name, Meeting Day, Frequency (weekly or fortnightly), Officer Email."""
    from ..models import LendingGroup, StaffUser

    res = ImportResult()
    existing = {g.group_no: g for g in s.scalars(select(LendingGroup).where(LendingGroup.institution_id == institution_id))}
    officers = {u.email: u.id for u in s.scalars(select(StaffUser).where(StaffUser.institution_id == institution_id,
                                                                          StaffUser.is_active.is_(True)))}
    for i, row in enumerate(read_rows(content), start=2):
        no = pick(row, "group_no", "group_number", "group_code", "group_id").upper()
        name = pick(row, "group_name", "name")
        if not no or not name:
            res.rejected.append(f"row {i}: missing group number or name")
            continue
        long = _too_long(row, group_no=(no, 40), name=(name, 200))
        day_typed = pick(row, "meeting_day", "day").strip()
        day = day_typed.lower()
        freq = (pick(row, "frequency", "meeting_frequency") or "weekly").strip().lower()
        email = pick(row, "officer_email", "loan_officer_email", "officer").strip().lower()
        problem = (long or (f"meeting day '{day_typed}' is not a day of the week" if day and day[:3] not in [d[:3] for d in _DAYS] else None)
                   or (f"frequency '{freq}' should be weekly or fortnightly" if freq not in ("weekly", "fortnightly") else None)
                   or (f"no active staff member with email {email}" if email and email not in officers else None))
        if problem:
            res.rejected.append(f"row {i}: {no} {problem}")
            continue
        g = existing.get(no)
        if g is None:
            g = LendingGroup(institution_id=institution_id, group_no=no, name=name, status="active",
                             created_at=datetime.now(timezone.utc).replace(tzinfo=None))
            s.add(g)
            existing[no] = g
            res.created += 1
        else:
            res.skipped_duplicates += 1
        g.name, g.frequency = name, freq
        g.meeting_day = next((n for n, d in enumerate(_DAYS) if day and d.startswith(day[:3])), g.meeting_day)
        g.officer_user_id = officers.get(email, g.officer_user_id)
    s.commit()
    return res


def import_group_members(s: Session, institution_id: int, content) -> ImportResult:
    """Group membership: Group No, Member No, Role (member, chair, treasurer or secretary). A member is in one
    active group; a row moving them to another group is refused (they must leave the first one in the console)."""
    from ..models import GroupMember, LendingGroup

    res = ImportResult()
    groups = {g.group_no: g for g in s.scalars(select(LendingGroup).where(LendingGroup.institution_id == institution_id))}
    members = {m.member_no: m for m in s.scalars(select(Member).where(Member.institution_id == institution_id))}
    current = {gm.member_id: gm for gm in s.scalars(select(GroupMember).where(
        GroupMember.institution_id == institution_id, GroupMember.left_on.is_(None)))}
    today = datetime.now(timezone.utc).date()
    for i, row in enumerate(read_rows(content), start=2):
        g = groups.get(pick(row, "group_no", "group_number", "group_code", "group_id").upper())
        m = members.get(pick(row, "member_no", "member_number", "memberno"))
        role = (pick(row, "role", "position") or "member").strip().lower().replace(" ", "_")
        if g is None or m is None:
            res.rejected.append(f"row {i}: unknown group or member")
            continue
        if role not in ("member", "chair", "treasurer", "secretary", "group_account"):
            res.rejected.append(f"row {i}: {m.member_no} role '{role}' should be member, chair, treasurer, secretary "
                                f"or group account")
            continue
        if role == "group_account" and any(x.group_id == g.id and x.role == role and x.member_id != m.id
                                           for x in current.values()):
            res.rejected.append(f"row {i}: group {g.group_no} already has its own account")
            continue
        gm = current.get(m.id)
        if gm and gm.group_id != g.id:
            res.rejected.append(f"row {i}: {m.member_no} is already in another group; they must leave it first")
            continue
        if gm:
            gm.role = role
            res.skipped_duplicates += 1
        else:
            gm = GroupMember(institution_id=institution_id, group_id=g.id, member_id=m.id, role=role, joined_on=today)
            s.add(gm)
            current[m.id] = gm
            res.created += 1
    s.commit()
    return res
