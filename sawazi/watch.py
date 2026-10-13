"""Early warnings: employers that remit late or short, payments stuck in suspense, and integrity checks.

Everything here is read-only and worked out from records Sawazi already holds. Integrity findings are prompts for a
person to look, never conclusions: a shared phone is often a family, a late-night clearance often a busy month end.
"""
from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth
from .auth import require
from .db import get_session
from .models import (AuditEvent, CheckoffRemittance, CheckoffSchedule, ExceptionItem, LoanApplication, LoanDecision,
                     Member, StaffUser, Transaction)
from .network import id_key

router = APIRouter(tags=["watch"])

AGE_BUCKETS = ((0, 7, "0-7 days"), (8, 30, "8-30 days"), (31, 90, "31-90 days"), (91, 10**6, "over 90 days"))
NAIROBI = timedelta(hours=3)
MONEY_ACTIONS = ("suspense.clear", "suspense.split")


# ---------------------------------------------------------------- employers

def _period_end(period: str) -> date:
    y, m = map(int, period.split("-"))
    return date(y, m, calendar.monthrange(y, m)[1])


def employer_scorecard(s: Session, institution_id: int, periods: int = 6) -> list[dict]:
    """Each employer's last few check-off months: expected, remitted, the gap, members left off, days late."""
    sched = s.execute(select(CheckoffSchedule.employer, CheckoffSchedule.period, CheckoffSchedule.member_id,
                             CheckoffSchedule.expected_cents).where(CheckoffSchedule.institution_id == institution_id)).all()
    rem = s.execute(select(CheckoffRemittance.employer, CheckoffRemittance.period, CheckoffRemittance.member_id,
                           CheckoffRemittance.remitted_cents, CheckoffRemittance.received_on)
                    .where(CheckoffRemittance.institution_id == institution_id)).all()
    cells: dict[tuple[str, str], dict] = defaultdict(lambda: {"expected": 0, "remitted": 0, "due": set(), "paid": set(),
                                                              "received": None})
    for emp, per, mid, cents in sched:
        c = cells[(emp, per)]
        c["expected"] += cents
        c["due"].add(mid)
    for emp, per, mid, cents, on in rem:
        c = cells[(emp, per)]
        c["remitted"] += cents
        if mid:
            c["paid"].add(mid)
        if on and (c["received"] is None or on > c["received"]):
            c["received"] = on
    out: dict[str, list] = defaultdict(list)
    for (emp, per), c in cells.items():
        late = (c["received"] - _period_end(per)).days if c["received"] else None
        out[emp].append({"period": per, "expected_kes": c["expected"] / 100, "remitted_kes": c["remitted"] / 100,
                         "gap_kes": (c["expected"] - c["remitted"]) / 100, "members_left_off": len(c["due"] - c["paid"]),
                         "received_on": c["received"], "days_late": max(late, 0) if late is not None else None})
    result = []
    for emp, rows in out.items():
        rows = sorted(rows, key=lambda r: r["period"], reverse=True)[:periods]
        latest = rows[0]
        result.append({"employer": emp, "latest": latest, "months": rows,
                       "months_short": sum(1 for r in rows if r["gap_kes"] > 0),
                       "months_late": sum(1 for r in rows if (r["days_late"] or 0) > 10)})
    result.sort(key=lambda e: (-e["latest"]["gap_kes"], e["employer"]))
    return result


@router.get("/institutions/{institution_id}/employers", dependencies=[Depends(require("read"))])
def employers(institution_id: int, s: Session = Depends(get_session)):
    return employer_scorecard(s, institution_id)


# ---------------------------------------------------------------- suspense ageing

def suspense_ageing(s: Session, institution_id: int, today: date) -> dict:
    rows = s.execute(select(ExceptionItem, Transaction.txn_time).outerjoin(
        Transaction, ExceptionItem.transaction_id == Transaction.id).where(
        ExceptionItem.institution_id == institution_id, ExceptionItem.kind == "suspense",
        ExceptionItem.status == "open")).all()
    buckets = [{"label": label, "count": 0, "kes": 0.0} for *_, label in AGE_BUCKETS]
    oldest = []
    for e, when in rows:
        age = (today - (when or e.created_at).date()).days
        for b, (lo, hi, _) in zip(buckets, AGE_BUCKETS):
            if lo <= age <= hi:
                b["count"] += 1
                b["kes"] += e.amount_cents / 100
        oldest.append({"id": e.id, "age_days": age, "kes": e.amount_cents / 100, "detail": e.detail})
    oldest.sort(key=lambda x: -x["age_days"])
    return {"buckets": buckets, "over_30": {"count": sum(b["count"] for b in buckets[2:]),
                                            "kes": sum(b["kes"] for b in buckets[2:])}, "oldest": oldest[:10]}


@router.get("/institutions/{institution_id}/suspense/ageing", dependencies=[Depends(require("read"))])
def suspense_age(institution_id: int, s: Session = Depends(get_session)):
    return suspense_ageing(s, institution_id, auth.utcnow().date())


# ---------------------------------------------------------------- integrity checks

def integrity(s: Session, institution_id: int, now: datetime, days: int = 90) -> dict:
    since = now - timedelta(days=days)
    findings: list[dict] = []

    def add(kind, severity, message):
        findings.append({"kind": kind, "severity": severity, "message": message})

    members = list(s.scalars(select(Member).where(Member.institution_id == institution_id)))
    by_id, by_phone = defaultdict(list), defaultdict(list)
    for m in members:
        if id_key(m.id_number):
            by_id[id_key(m.id_number)].append(m)
        if m.phone:
            by_phone[m.phone].append(m)
    for ms in by_id.values():
        if len(ms) > 1:
            add("duplicate_id", "high", f"One ID number on {len(ms)} member records: "
                + ", ".join(f"{m.member_no} {m.name}" for m in ms) + ". Check for a duplicate or a borrowed identity.")
    for phone, ms in by_phone.items():
        if len(ms) >= 3:
            add("shared_phone", "medium", f"Phone {phone} is on {len(ms)} members: "
                + ", ".join(m.member_no for m in ms[:6]) + ". Often a family; check if loans or payments move between them.")

    # approvals over failed or unknown checks, per approver
    approvers: dict[int, Counter] = defaultdict(Counter)
    for d, a in s.execute(select(LoanDecision, LoanApplication).join(
            LoanApplication, LoanDecision.application_id == LoanApplication.id).where(
            LoanDecision.institution_id == institution_id, LoanDecision.decision == "approve", LoanDecision.at >= since)):
        c = approvers[d.user_id]
        c["approved"] += 1
        if a.override_reason:
            c["overrides"] += 1
            if a.submitted_at and (d.at - a.submitted_at) < timedelta(minutes=30):
                add("quick_override", "medium", f"Application SWZ-{a.id} was approved over failed or unknown checks "
                    f"{int((d.at - a.submitted_at).total_seconds() // 60)} minutes after it was submitted.")
    overrides = []
    for uid, c in approvers.items():
        u = s.get(StaffUser, uid)
        rate = round(100 * c["overrides"] / c["approved"], 1)
        overrides.append({"approver": u.name if u else f"user {uid}", "approved": c["approved"],
                          "overrides": c["overrides"], "override_pct": rate})
        if c["overrides"] >= 3 and rate >= 30:
            add("override_habit", "medium", f"{u.name if u else uid} approved {c['overrides']} of {c['approved']} "
                f"loans over failed or unknown checks in {days} days.")
    overrides.sort(key=lambda r: -r["override_pct"])

    # money cleared by hand: to insiders, and outside working hours (Nairobi time)
    insiders = {m.member_no: m for m in members if m.insider}
    for ev in s.scalars(select(AuditEvent).where(AuditEvent.institution_id == institution_id, AuditEvent.at >= since,
                                                 AuditEvent.action.in_(MONEY_ACTIONS))):
        after = ev.after or {}
        nos = [after.get("member_no")] + [x.get("member_no") for x in after.get("shares", [])]
        for no in filter(None, nos):
            if no in insiders:
                add("insider_clearance", "high", f"{ev.actor_name} cleared a suspense payment to {no} "
                    f"{insiders[no].name}, an insider, on {ev.at:%Y-%m-%d}. A second person should confirm it.")
        local = ev.at + NAIROBI
        if local.hour < 7 or local.hour >= 20 or local.weekday() == 6:
            add("after_hours", "low", f"{ev.actor_name} cleared suspense at {local:%a %d %b %H:%M} (Nairobi time).")
    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: order[f["severity"]])
    return {"days": days, "findings": findings, "approvers": overrides}


@router.get("/institutions/{institution_id}/integrity", dependencies=[Depends(require("read"))])
def integrity_checks(institution_id: int, s: Session = Depends(get_session)):
    return integrity(s, institution_id, auth.utcnow())


def figures(s: Session, institution_id: int) -> dict:
    """For snapshots and the board pack (cents)."""
    age = suspense_ageing(s, institution_id, auth.utcnow().date())
    ints = integrity(s, institution_id, auth.utcnow())
    short = [{"employer": e["employer"], "period": e["latest"]["period"], "gap_cents": round(e["latest"]["gap_kes"] * 100)}
             for e in employer_scorecard(s, institution_id) if e["latest"]["gap_kes"] > 0]
    return {"watch": {"suspense_over_30": {"count": age["over_30"]["count"], "cents": round(age["over_30"]["kes"] * 100)},
                      "integrity_high": sum(f["severity"] == "high" for f in ints["findings"]),
                      "employers_short": short[:5]}}
