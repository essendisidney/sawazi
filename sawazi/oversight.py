"""Promises to pay, loan officers' books, and insider loans.

Promises: when a member in arrears promises to pay an amount by a date, the officer records it. Sawazi checks it
against the payments actually allocated to that loan, so "kept" or "broken" is never a matter of opinion, and the
collections queue leaves the loan alone until the date passes.

Officers: each loan's officer comes from the core system's loans export; the book per officer shows outstanding,
arrears, PAR 30, what came in this month and how many promises were kept.

Insiders: members flagged in the members export as board, supervisory committee, staff, or related to them. Their
loans are listed for the board and the auditors; Sawazi never decides what the insider-lending policy allows.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, auth
from .auth import Principal, require
from .db import get_session
from .models import Allocation, Loan, Member, PromiseToPay, Transaction

router = APIRouter(tags=["oversight"])

INSIDERS = {"board": "Board member", "supervisory": "Supervisory committee", "staff": "Staff",
            "related": "Related to board or staff"}
MAX_PROMISE_DAYS = 60


def paid_between(s: Session, loan_id: int, start: date, end: date) -> int:
    """Money allocated to the loan from payments made on start..end (inclusive)."""
    return s.scalar(select(func.coalesce(func.sum(Allocation.amount_cents), 0))
                    .join(Transaction, Allocation.transaction_id == Transaction.id)
                    .where(Allocation.loan_id == loan_id, Transaction.status == "allocated",
                           Transaction.txn_time >= datetime.combine(start, datetime.min.time()),
                           Transaction.txn_time < datetime.combine(end + timedelta(days=1), datetime.min.time()))) or 0


def promise_state(s: Session, p: PromiseToPay, today: date) -> tuple[str, int]:
    """(state, paid cents): cancelled | kept | open | part_paid | broken."""
    paid = paid_between(s, p.loan_id, p.made_on, min(p.due_on, today))
    if p.cancelled_at:
        return "cancelled", paid
    if paid >= p.amount_cents:
        return "kept", paid
    if today <= p.due_on:
        return "open", paid
    return ("part_paid" if paid else "broken"), paid


def open_promise_loans(s: Session, institution_id: int, today: date) -> set[int]:
    """Loans with a promise still running (not yet due, not yet kept): collections leaves them alone till then."""
    out = set()
    for p in s.scalars(select(PromiseToPay).where(PromiseToPay.institution_id == institution_id,
                                                  PromiseToPay.cancelled_at.is_(None), PromiseToPay.due_on >= today)):
        if promise_state(s, p, today)[0] == "open":
            out.add(p.loan_id)
    return out


def _promise_out(s: Session, p: PromiseToPay, today: date) -> dict:
    ln = s.get(Loan, p.loan_id)
    m = s.get(Member, ln.member_id)
    state, paid = promise_state(s, p, today)
    return {"id": p.id, "loan_no": ln.loan_no, "member_no": m.member_no, "member": m.name, "officer": ln.officer,
            "amount_kes": p.amount_cents / 100, "paid_kes": paid / 100, "made_on": p.made_on, "due_on": p.due_on,
            "state": state, "note": p.note}


class PromiseIn(BaseModel):
    loan_no: str = Field(min_length=1, max_length=40)
    amount_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    due_on: date
    note: str | None = Field(default=None, max_length=500)


@router.post("/institutions/{institution_id}/promises")
def record_promise(institution_id: int, body: PromiseIn, s: Session = Depends(get_session),
                   who: Principal = Depends(require("collections"))):
    today = auth.utcnow().date()
    ln = s.scalar(select(Loan).where(Loan.institution_id == institution_id, Loan.loan_no == body.loan_no.strip()))
    if ln is None:
        raise HTTPException(404, "loan not found")
    if not today <= body.due_on <= today + timedelta(days=MAX_PROMISE_DAYS):
        raise HTTPException(422, f"the promise date must be between today and {MAX_PROMISE_DAYS} days from now")
    if ln.id in open_promise_loans(s, institution_id, today):
        raise HTTPException(409, f"loan {ln.loan_no} already has a promise running: cancel it first")
    p = PromiseToPay(institution_id=institution_id, loan_id=ln.id, amount_cents=int(body.amount_kes * 100),
                     made_on=today, due_on=body.due_on, note=body.note,
                     recorded_by_user_id=who.id if who.kind == "user" else None, created_at=auth.utcnow())
    s.add(p)
    s.flush()
    audit.record(s, who, "promise.record", "promise", p.id,
                 after={"loan_no": ln.loan_no, "amount_cents": p.amount_cents, "due_on": str(p.due_on)})
    s.commit()
    return _promise_out(s, p, today)


@router.post("/institutions/{institution_id}/promises/{pid}/cancel")
def cancel_promise(institution_id: int, pid: int, s: Session = Depends(get_session),
                   who: Principal = Depends(require("collections"))):
    p = s.scalar(select(PromiseToPay).where(PromiseToPay.id == pid, PromiseToPay.institution_id == institution_id))
    if p is None:
        raise HTTPException(404, "promise not found")
    if p.cancelled_at is None:
        p.cancelled_at = auth.utcnow()
        audit.record(s, who, "promise.cancel", "promise", p.id)
        s.commit()
    return _promise_out(s, p, auth.utcnow().date())


@router.get("/institutions/{institution_id}/promises", dependencies=[Depends(require("read"))])
def list_promises(institution_id: int, days: int = Query(60, ge=1, le=366), s: Session = Depends(get_session)):
    today = auth.utcnow().date()
    rows = s.scalars(select(PromiseToPay).where(PromiseToPay.institution_id == institution_id,
                                                PromiseToPay.made_on >= today - timedelta(days=days))
                     .order_by(PromiseToPay.due_on))
    return [_promise_out(s, p, today) for p in rows]


@router.get("/institutions/{institution_id}/officers", dependencies=[Depends(require("read"))])
def officers(institution_id: int, s: Session = Depends(get_session)):
    """Each loan officer's book (officer as the core system names them), worst PAR first."""
    today = auth.utcnow().date()
    month_start = today.replace(day=1)
    books: dict[str, dict] = {}
    for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.status == "active")):
        b = books.setdefault(ln.officer or "", {"loans": 0, "outstanding": 0, "arrears": 0, "at_risk": 0, "ids": []})
        b["loans"] += 1
        b["outstanding"] += ln.balance_cents
        b["arrears"] += ln.arrears_cents
        b["at_risk"] += ln.balance_cents if ln.days_in_arrears > 30 else 0
        b["ids"].append(ln.id)
    promises: dict[str, dict] = {}
    for p in s.scalars(select(PromiseToPay).where(PromiseToPay.institution_id == institution_id,
                                                  PromiseToPay.made_on >= today - timedelta(days=90))):
        st = promise_state(s, p, today)[0]
        c = promises.setdefault(s.get(Loan, p.loan_id).officer or "", {"kept": 0, "broken": 0, "open": 0})
        if st == "kept":
            c["kept"] += 1
        elif st in ("broken", "part_paid"):
            c["broken"] += 1
        elif st == "open":
            c["open"] += 1
    out = []
    for name, b in books.items():
        collected = s.scalar(select(func.coalesce(func.sum(Allocation.amount_cents), 0))
                             .join(Transaction, Allocation.transaction_id == Transaction.id)
                             .where(Allocation.institution_id == institution_id, Allocation.loan_id.in_(b["ids"]),
                                    Transaction.status == "allocated",
                                    Transaction.txn_time >= datetime.combine(month_start, datetime.min.time())))
        pr = promises.get(name, {"kept": 0, "broken": 0, "open": 0})
        out.append({"officer": name or None, "loans": b["loans"], "outstanding_kes": b["outstanding"] / 100,
                    "arrears_kes": b["arrears"] / 100,
                    "par30_pct": round(100 * b["at_risk"] / b["outstanding"], 1) if b["outstanding"] else 0.0,
                    "collected_this_month_kes": (collected or 0) / 100,
                    "promises_kept": pr["kept"], "promises_broken": pr["broken"], "promises_open": pr["open"]})
    out.sort(key=lambda r: (-r["par30_pct"], r["officer"] or "~"))
    return out


def insider_figures(s: Session, institution_id: int) -> dict | None:
    """Insider loans in cents (snapshots and the board pack). None when no insider has a loan."""
    rows = s.execute(select(Loan, Member).join(Member, Loan.member_id == Member.id).where(
        Loan.institution_id == institution_id, Loan.status == "active", Member.insider.is_not(None))).all()
    if not rows:
        return None
    return {"loans": len(rows), "balance_cents": sum(ln.balance_cents for ln, _ in rows),
            "behind": sum(1 for ln, _ in rows if ln.days_in_arrears > 0),
            "by_kind": {k: sum(ln.balance_cents for ln, m in rows if m.insider == k) for k in INSIDERS}}


@router.get("/institutions/{institution_id}/insiders", dependencies=[Depends(require("read"))])
def insiders(institution_id: int, s: Session = Depends(get_session)):
    rows = s.execute(select(Loan, Member).join(Member, Loan.member_id == Member.id).where(
        Loan.institution_id == institution_id, Loan.status == "active", Member.insider.is_not(None))
        .order_by(Loan.balance_cents.desc())).all()
    return {"insiders": s.scalar(select(func.count()).select_from(Member).where(
                Member.institution_id == institution_id, Member.insider.is_not(None))),
            "loans": [{"member_no": m.member_no, "member": m.name, "insider": INSIDERS[m.insider], "loan_no": ln.loan_no,
                       "product": ln.product, "balance_kes": ln.balance_cents / 100, "arrears_kes": ln.arrears_cents / 100,
                       "days_in_arrears": ln.days_in_arrears, "officer": ln.officer} for ln, m in rows]}
