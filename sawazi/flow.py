"""How things move: arrears from one loans upload to the next, payments that keep missing their reference, and how
long loans take from application to money.

Roll rates: every snapshot (each loans upload) records each loan's days in arrears as at that date, so two dates can
be compared loan by loan: of the loans 1-30 days behind last time, how many are now current, still 1-30, or worse.

Reference coaching: a member whose payment had to be cleared from suspense by hand paid without a usable reference.
Members who keep doing it are listed with a drafted, plain message telling them exactly what to type next time.

Turnaround: the time each step of a loan takes, and the applications stuck at a step longer than they should be.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import median

from fastapi import APIRouter, Depends, Query
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import auth
from .auth import require
from .db import get_session
from .models import (AuditEvent, Guarantee, Institution, LoanApplication, Loan, LoanArrearsHistory, Member)

router = APIRouter(tags=["flow"])

BUCKETS = ("current", "1-30", "31-60", "61-90", "over 90", "closed")
STUCK_DAYS = {"submitted": 3, "approved": 2, "exported": 5}  # waiting for a decision, the hand-over, the core


def bucket(days: int, status: str) -> str:
    if status != "active":
        return "closed"  # repaid or written off: out of the active book
    if days <= 0:
        return "current"
    return "1-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "over 90"


def record_history(s: Session, institution_id: int, as_of: date) -> int:
    """Each loan's arrears as at this date (same date again replaces it). Called with every snapshot."""
    s.execute(delete(LoanArrearsHistory).where(LoanArrearsHistory.institution_id == institution_id,
                                               LoanArrearsHistory.as_of == as_of))
    loans = list(s.scalars(select(Loan).where(Loan.institution_id == institution_id)))
    s.add_all(LoanArrearsHistory(institution_id=institution_id, loan_id=ln.id, as_of=as_of, status=ln.status,
                                 days_in_arrears=ln.days_in_arrears, balance_cents=ln.balance_cents,
                                 arrears_cents=ln.arrears_cents) for ln in loans)
    return len(loans)


def roll_rates(s: Session, institution_id: int, start: date | None = None, end: date | None = None) -> dict | None:
    """Loan-by-loan movement between two recorded dates (default: the two latest). None until two exist."""
    dates = sorted(set(s.scalars(select(LoanArrearsHistory.as_of).where(
        LoanArrearsHistory.institution_id == institution_id))), reverse=True)
    if end is None or start is None:
        if len(dates) < 2:
            return None
        end, start = dates[0], dates[1]

    def at(d):
        return {h.loan_id: h for h in s.scalars(select(LoanArrearsHistory).where(
            LoanArrearsHistory.institution_id == institution_id, LoanArrearsHistory.as_of == d))}

    before, after = at(start), at(end)
    matrix = {b: Counter() for b in BUCKETS[:-1]}
    balances = {b: 0 for b in BUCKETS[:-1]}
    for loan_id, h in before.items():
        frm = bucket(h.days_in_arrears, h.status)
        if frm == "closed":
            continue
        a = after.get(loan_id)
        to = bucket(a.days_in_arrears, a.status) if a else "closed"
        matrix[frm][to] += 1
        balances[frm] += h.balance_cents
    rows = []
    for frm in BUCKETS[:-1]:
        n = sum(matrix[frm].values())
        worse = sum(c for to, c in matrix[frm].items() if to != "closed" and BUCKETS.index(to) > BUCKETS.index(frm))
        cured = matrix[frm]["current"] + matrix[frm]["closed"] if frm != "current" else 0
        rows.append({"from": frm, "loans": n, "balance_kes": balances[frm] / 100,
                     "to": {to: matrix[frm][to] for to in BUCKETS},
                     "rolled_worse_pct": round(100 * worse / n, 1) if n else 0.0,
                     "cured_pct": round(100 * cured / n, 1) if n else 0.0})
    new = sum(1 for loan_id in after if loan_id not in before)
    return {"start": start, "end": end, "rows": rows, "new_loans": new}


@router.get("/institutions/{institution_id}/roll-rates", dependencies=[Depends(require("read"))])
def roll_rates_view(institution_id: int, start: date | None = None, end: date | None = None,
                    s: Session = Depends(get_session)):
    dates = sorted(set(s.scalars(select(LoanArrearsHistory.as_of).where(
        LoanArrearsHistory.institution_id == institution_id))), reverse=True)
    return {"dates": dates[:24], "rates": roll_rates(s, institution_id, start, end)}


# ---------------------------------------------------------------- reference coaching

def coaching(s: Session, institution_id: int, since: datetime, min_times: int = 2) -> list[dict]:
    inst = s.get(Institution, institution_id)
    counts: dict[str, list[datetime]] = defaultdict(list)
    for ev in s.scalars(select(AuditEvent).where(AuditEvent.institution_id == institution_id,
                                                 AuditEvent.action == "suspense.clear", AuditEvent.at >= since)):
        no = (ev.after or {}).get("member_no")
        if no:
            counts[no].append(ev.at)
    out = []
    for no, times in counts.items():
        if len(times) < min_times:
            continue
        m = s.scalar(select(Member).where(Member.institution_id == institution_id, Member.member_no == no))
        if m is None:
            continue
        loans = list(s.scalars(select(Loan.loan_no).where(Loan.institution_id == institution_id,
                                                          Loan.member_id == m.id, Loan.status == "active")))
        where = f"Paybill {inst.paybill}, " if inst.paybill else ""
        account = loans[0] if len(loans) == 1 else m.member_no
        first = m.name.split()[0].title() if m.name else "Member"
        out.append({"member_no": no, "name": m.name, "phone": m.phone, "times": len(times),
                    "last": max(times), "account": account,
                    "message": f"Dear {first}, thank you for your payments to {inst.name}. Some did not carry your "
                               f"account number, so they took longer to reach your account. Next time please pay to "
                               f"{where}account {account}."})
    out.sort(key=lambda r: (-r["times"], r["member_no"]))
    return out


@router.get("/institutions/{institution_id}/reference-coaching", dependencies=[Depends(require("read"))])
def reference_coaching(institution_id: int, days: int = Query(180, ge=30, le=366), s: Session = Depends(get_session)):
    """Members whose payments had to be cleared from suspense by hand at least twice, with a message to send."""
    return coaching(s, institution_id, auth.utcnow() - timedelta(days=days))


# ---------------------------------------------------------------- turnaround

def _days(a: datetime | None, b: datetime | None) -> float | None:
    return round((b - a).total_seconds() / 86400, 1) if a and b else None


def turnaround(s: Session, institution_id: int, now: datetime, days: int = 90) -> dict:
    apps = list(s.scalars(select(LoanApplication).where(LoanApplication.institution_id == institution_id,
                                                        LoanApplication.created_at >= now - timedelta(days=days))))
    steps = {"capture_to_submit": [], "submit_to_decision": [], "decision_to_handover": [], "handover_to_money": [],
             "submit_to_money": []}
    for a in apps:
        for key, x, y in (("capture_to_submit", a.created_at, a.submitted_at),
                          ("submit_to_decision", a.submitted_at, a.decided_at),
                          ("decision_to_handover", a.decided_at, a.exported_at),
                          ("handover_to_money", a.exported_at, a.disbursed_at),
                          ("submit_to_money", a.submitted_at, a.disbursed_at)):
            d = _days(x, y)
            if d is not None:
                steps[key].append(d)
    since = {"submitted": "submitted_at", "approved": "decided_at", "exported": "exported_at"}
    stuck = []
    for a in s.scalars(select(LoanApplication).where(LoanApplication.institution_id == institution_id,
                                                     LoanApplication.status.in_(tuple(STUCK_DAYS)))):
        t = getattr(a, since[a.status])
        waited = _days(t, now)
        if waited is not None and waited > STUCK_DAYS[a.status]:
            m = s.get(Member, a.member_id)
            stuck.append({"ref": f"SWZ-{a.id}", "id": a.id, "member": m.name, "member_no": m.member_no,
                          "kes": a.amount_cents / 100, "status": a.status, "waiting_days": waited})
    waiting_guarantors = []
    for g in s.scalars(select(Guarantee).where(Guarantee.institution_id == institution_id,
                                               Guarantee.status == "requested", Guarantee.expires_at > now,
                                               Guarantee.requested_at < now - timedelta(days=2))):
        gm = s.get(Member, g.guarantor_member_id)
        waiting_guarantors.append({"application": f"SWZ-{g.application_id}", "id": g.application_id,
                                   "guarantor": gm.name, "member_no": gm.member_no,
                                   "waiting_days": _days(g.requested_at, now)})
    stuck.sort(key=lambda x: -x["waiting_days"])
    return {"days": days, "applications": len(apps),
            "median_days": {k: (round(median(v), 1) if v else None) for k, v in steps.items()},
            "stuck": stuck, "guarantors_waiting": sorted(waiting_guarantors, key=lambda x: -x["waiting_days"])}


@router.get("/institutions/{institution_id}/turnaround", dependencies=[Depends(require("read"))])
def turnaround_view(institution_id: int, s: Session = Depends(get_session)):
    return turnaround(s, institution_id, auth.utcnow())
