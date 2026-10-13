"""Recovery of defaulted loans: who is chasing each one, what has come back, and the guarantors' deposits.

When a loan defaults the SACCO sends a demand notice, then hands it to a debt collector (EDC), an auctioneer or the
court, and may use the guarantors' deposits to pay it down. Staff record here what the core system and the agent
report; Sawazi never moves money. Money recovered first goes back to the guarantors whose deposits were used, so a
recovery shows how much of that refund is due and not yet made.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, auth, guarantors
from .auth import Principal, require
from .db import get_session
from .models import CoreGuarantee, Guarantee, LoanApplication, Loan, Member, Recovery, RecoveryGuarantor

router = APIRouter(tags=["recovery"])

STAGES = ("demand_notice", "debt_collector", "auctioneer", "court", "recovered", "closed")
OPEN = STAGES[:4]


def _cents(kes: Decimal) -> int:
    return int(kes * 100)


def _recovery(s: Session, institution_id: int, rid: int, lock: bool = False) -> Recovery:
    q = select(Recovery).where(Recovery.id == rid, Recovery.institution_id == institution_id)
    r = s.scalar(q.with_for_update() if lock else q)
    if r is None:
        raise HTTPException(404, "recovery not found")
    return r


def _guarantor_rows(s: Session, r: Recovery) -> list[RecoveryGuarantor]:
    return list(s.scalars(select(RecoveryGuarantor).where(RecoveryGuarantor.institution_id == r.institution_id,
                                                          RecoveryGuarantor.recovery_id == r.id)
                          .order_by(RecoveryGuarantor.id)))


def refund_due_cents(r: Recovery, rows: list[RecoveryGuarantor]) -> int:
    """Recovered money owed back to guarantors: up to what was taken from them, less what was already refunded."""
    used, refunded = sum(g.used_cents for g in rows), sum(g.refunded_cents for g in rows)
    return max(min(r.recovered_cents, used) - refunded, 0)


def _out(s: Session, r: Recovery, today: date) -> dict:
    ln = s.get(Loan, r.loan_id)
    m = s.get(Member, ln.member_id)
    rows = _guarantor_rows(s, r)
    return {"id": r.id, "loan_no": ln.loan_no, "member_no": m.member_no, "member": m.name,
            "balance_kes": ln.balance_cents / 100, "days_in_arrears": ln.days_in_arrears, "loan_status": ln.status,
            "stage": r.stage, "agent": r.agent, "handed_over_on": r.handed_over_on,
            "days_with_agent": (today - r.handed_over_on).days if r.handed_over_on and r.stage in OPEN else None,
            "recovered_kes": r.recovered_cents / 100, "note": r.note,
            "deposits_used_kes": sum(g.used_cents for g in rows) / 100,
            "refunded_kes": sum(g.refunded_cents for g in rows) / 100,
            "refund_due_kes": refund_due_cents(r, rows) / 100, "updated_at": r.updated_at}


def figures(s: Session, institution_id: int) -> dict | None:
    """For snapshots and the board pack, in cents. None when nothing is in recovery."""
    recs = list(s.scalars(select(Recovery).where(Recovery.institution_id == institution_id)))
    if not recs:
        return None
    open_ = [r for r in recs if r.stage in OPEN]
    due = [refund_due_cents(r, _guarantor_rows(s, r)) for r in recs]
    return {"open": len(open_), "with_agent": sum(1 for r in open_ if r.agent),
            "recovered_cents": sum(r.recovered_cents for r in recs), "refund_due_cents": sum(due),
            "refunds_waiting": sum(1 for d in due if d)}


@router.get("/institutions/{institution_id}/recoveries", dependencies=[Depends(require("read"))])
def list_recoveries(institution_id: int, s: Session = Depends(get_session)):
    today = auth.utcnow().date()
    recs = s.scalars(select(Recovery).where(Recovery.institution_id == institution_id).order_by(Recovery.id.desc()))
    return [_out(s, r, today) for r in recs]


@router.get("/institutions/{institution_id}/recoveries/{rid}", dependencies=[Depends(require("read"))])
def get_recovery(institution_id: int, rid: int, s: Session = Depends(get_session)):
    r = _recovery(s, institution_id, rid)
    rows = _guarantor_rows(s, r)
    taken = {g.member_id for g in rows}
    # the loan's guarantors, from Sawazi and the core system, to pick from
    backers = {gm.id: (gm, amt) for gm, amt in s.execute(
        select(Member, Guarantee.amount_cents).join(Guarantee, Guarantee.guarantor_member_id == Member.id)
        .join(LoanApplication, Guarantee.application_id == LoanApplication.id)
        .where(Guarantee.institution_id == institution_id, LoanApplication.disbursed_loan_id == r.loan_id,
               Guarantee.status == "accepted"))}
    for gm, amt in s.execute(select(Member, CoreGuarantee.amount_cents)
                             .join(CoreGuarantee, CoreGuarantee.guarantor_member_id == Member.id)
                             .where(CoreGuarantee.institution_id == institution_id, CoreGuarantee.loan_id == r.loan_id,
                                    CoreGuarantee.status == "active", guarantors.core_counts())):
        backers.setdefault(gm.id, (gm, amt))
    return _out(s, r, auth.utcnow().date()) | {
        "guarantors": [{"id": g.id, "member_no": s.get(Member, g.member_id).member_no,
                        "name": s.get(Member, g.member_id).name, "used_kes": g.used_cents / 100, "used_on": g.used_on,
                        "refunded_kes": g.refunded_cents / 100, "refunded_on": g.refunded_on} for g in rows],
        "loan_guarantors": [{"member_no": gm.member_no, "name": gm.name, "pledged_kes": amt / 100}
                            for gm, amt in backers.values() if gm.id not in taken]}


class RecoveryIn(BaseModel):
    stage: str = Field(default="demand_notice", pattern="^(" + "|".join(STAGES) + ")$")
    agent: str | None = Field(default=None, max_length=200, description="debt collector, auctioneer or advocate")
    handed_over_on: date | None = None
    recovered_kes: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2,
                                   description="total recovered so far, as the core system shows it")
    note: str | None = Field(default=None, max_length=2000)


class NewRecoveryIn(RecoveryIn):
    loan_no: str = Field(min_length=1, max_length=40)


def _apply(r: Recovery, body: RecoveryIn) -> None:
    if body.handed_over_on and body.handed_over_on > auth.utcnow().date():
        raise HTTPException(422, "the hand-over date is in the future")
    r.stage, r.agent, r.handed_over_on = body.stage, (body.agent or "").strip() or None, body.handed_over_on
    r.recovered_cents, r.note = _cents(body.recovered_kes), body.note
    r.updated_at = auth.utcnow()


def _fields(r: Recovery) -> dict:
    return {"stage": r.stage, "agent": r.agent, "handed_over_on": str(r.handed_over_on or ""),
            "recovered_cents": r.recovered_cents}


@router.post("/institutions/{institution_id}/recoveries")
def open_recovery(institution_id: int, body: NewRecoveryIn, s: Session = Depends(get_session),
                  who: Principal = Depends(require("recovery"))):
    ln = s.scalar(select(Loan).where(Loan.institution_id == institution_id, Loan.loan_no == body.loan_no.strip()))
    if ln is None:
        raise HTTPException(404, "loan not found")
    if s.scalar(select(Recovery.id).where(Recovery.institution_id == institution_id, Recovery.loan_id == ln.id)):
        raise HTTPException(409, f"loan {ln.loan_no} is already in recovery")
    r = Recovery(institution_id=institution_id, loan_id=ln.id, created_at=auth.utcnow())
    _apply(r, body)
    s.add(r)
    s.flush()
    audit.record(s, who, "recovery.open", "recovery", r.id, after={"loan_no": ln.loan_no, **_fields(r)})
    s.commit()
    return _out(s, r, auth.utcnow().date())


@router.put("/institutions/{institution_id}/recoveries/{rid}")
def update_recovery(institution_id: int, rid: int, body: RecoveryIn, s: Session = Depends(get_session),
                    who: Principal = Depends(require("recovery"))):
    r = _recovery(s, institution_id, rid, lock=True)
    before = _fields(r)
    _apply(r, body)
    after = _fields(r)
    audit.record(s, who, "recovery.update", "recovery", r.id, before={k: v for k, v in before.items() if after[k] != v},
                 after={k: v for k, v in after.items() if before[k] != v})
    s.commit()
    return _out(s, r, auth.utcnow().date())


class UsedIn(BaseModel):
    member_no: str = Field(min_length=1, max_length=40)
    used_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    used_on: date


class RefundIn(BaseModel):
    used_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    used_on: date
    refunded_kes: Decimal = Field(default=Decimal(0), ge=0, max_digits=14, decimal_places=2)
    refunded_on: date | None = None


@router.post("/institutions/{institution_id}/recoveries/{rid}/guarantors")
def record_deposits_used(institution_id: int, rid: int, body: UsedIn, s: Session = Depends(get_session),
                         who: Principal = Depends(require("recovery"))):
    """The core system used this guarantor's deposits towards the loan."""
    r = _recovery(s, institution_id, rid, lock=True)
    m = s.scalar(select(Member).where(Member.institution_id == institution_id,
                                      Member.member_no == body.member_no.strip().upper()))
    if m is None:
        raise HTTPException(404, "member not found")
    if m.id == s.get(Loan, r.loan_id).member_id:
        raise HTTPException(422, "that is the borrower, not a guarantor")
    if s.scalar(select(RecoveryGuarantor.id).where(RecoveryGuarantor.recovery_id == r.id,
                                                   RecoveryGuarantor.member_id == m.id)):
        raise HTTPException(409, f"{m.member_no} is already recorded on this recovery: edit that line")
    g = RecoveryGuarantor(institution_id=institution_id, recovery_id=r.id, member_id=m.id,
                          used_cents=_cents(body.used_kes), used_on=body.used_on, refunded_cents=0)
    s.add(g)
    r.updated_at = auth.utcnow()
    s.flush()
    audit.record(s, who, "recovery.deposits_used", "recovery", r.id,
                 after={"member_no": m.member_no, "used_cents": g.used_cents, "used_on": str(g.used_on)})
    s.commit()
    return get_recovery(institution_id, rid, s)


@router.put("/institutions/{institution_id}/recoveries/{rid}/guarantors/{gid}")
def record_refund(institution_id: int, rid: int, gid: int, body: RefundIn, s: Session = Depends(get_session),
                  who: Principal = Depends(require("recovery"))):
    """Correct what was used, or record what the core system has refunded to the guarantor."""
    r = _recovery(s, institution_id, rid, lock=True)
    g = s.scalar(select(RecoveryGuarantor).where(RecoveryGuarantor.id == gid, RecoveryGuarantor.recovery_id == r.id,
                                                 RecoveryGuarantor.institution_id == institution_id))
    if g is None:
        raise HTTPException(404, "guarantor line not found")
    used, refunded = _cents(body.used_kes), _cents(body.refunded_kes)
    if refunded > used:
        raise HTTPException(422, "the refund is more than the deposits used")
    if refunded and body.refunded_on is None:
        raise HTTPException(422, "give the date of the refund")
    before = {"used_cents": g.used_cents, "refunded_cents": g.refunded_cents}
    g.used_cents, g.used_on, g.refunded_cents = used, body.used_on, refunded
    g.refunded_on = body.refunded_on if refunded else None
    r.updated_at = auth.utcnow()
    audit.record(s, who, "recovery.refund", "recovery", r.id, before=before,
                 after={"member_no": s.get(Member, g.member_id).member_no, "used_cents": used, "refunded_cents": refunded})
    s.commit()
    return get_recovery(institution_id, rid, s)


def for_guarantor(s: Session, m: Member) -> list[dict]:
    """A guarantor's own deposits used on loans they backed, and what has come back (member app)."""
    out = []
    for g, r in s.execute(select(RecoveryGuarantor, Recovery).join(Recovery, RecoveryGuarantor.recovery_id == Recovery.id)
                          .where(RecoveryGuarantor.institution_id == m.institution_id, RecoveryGuarantor.member_id == m.id)
                          .order_by(RecoveryGuarantor.used_on.desc())):
        out.append({"for": s.get(Member, s.get(Loan, r.loan_id).member_id), "used_kes": g.used_cents / 100,
                    "used_on": g.used_on, "refunded_kes": g.refunded_cents / 100, "refunded_on": g.refunded_on,
                    "being_recovered": r.stage in OPEN})
    return out
