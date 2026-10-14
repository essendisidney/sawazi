"""Two working views: what one member of staff should do today, and how complete the core system's exports are.

My day gathers what is already in Sawazi for the signed-in person: promises falling due on their loans, their
applications waiting, decisions waiting for an approver, groups meeting today, loans in their book that just fell
behind, refunds owed to guarantors. Nothing new is stored.

Data quality counts the figures the core exports leave blank and says what each gap switches off, because Sawazi never
treats an unknown as zero: a member without a join date fails no check, but passes none either.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth
from .auth import PERMISSIONS, Principal, require
from .db import get_session
from .models import (GroupMember, LendingGroup, LoanApplication, Loan, Member, PromiseToPay, Recovery)
from .oversight import promise_state

router = APIRouter(tags=["my day"])


@router.get("/institutions/{institution_id}/my-day")
def my_day(institution_id: int, s: Session = Depends(get_session), who: Principal = Depends(require("read"))):
    now = auth.utcnow()
    today = now.date()
    me = (who.name or "").strip().lower()
    mine = lambda ln: me and (ln.officer or "").strip().lower() == me  # noqa: E731  (officer as the core names them)
    out: dict[str, list] = {"promises_due": [], "my_applications": [], "to_decide": [], "groups_today": [],
                            "newly_behind": [], "refunds_owed": []}

    for p in s.scalars(select(PromiseToPay).where(PromiseToPay.institution_id == institution_id,
                                                  PromiseToPay.cancelled_at.is_(None),
                                                  PromiseToPay.due_on.between(today - timedelta(days=3), today))):
        ln = s.get(Loan, p.loan_id)
        if p.recorded_by_user_id == who.id or mine(ln):
            state, paid = promise_state(s, p, today)
            if state != "kept":
                m = s.get(Member, ln.member_id)
                out["promises_due"].append({"loan_no": ln.loan_no, "member": m.name, "phone": m.phone,
                                            "amount_kes": p.amount_cents / 100, "paid_kes": paid / 100,
                                            "due_on": p.due_on, "state": state})

    for a in s.scalars(select(LoanApplication).where(LoanApplication.institution_id == institution_id,
                                                     LoanApplication.prepared_by_user_id == who.id,
                                                     LoanApplication.status.in_(("draft", "submitted", "approved")))):
        out["my_applications"].append({"id": a.id, "ref": f"SWZ-{a.id}", "member": s.get(Member, a.member_id).name,
                                       "kes": a.amount_cents / 100, "status": a.status})
    if who.role in PERMISSIONS["loan_approve"]:
        for a in s.scalars(select(LoanApplication).where(LoanApplication.institution_id == institution_id,
                                                         LoanApplication.status == "submitted",
                                                         LoanApplication.prepared_by_user_id != who.id)):
            out["to_decide"].append({"id": a.id, "ref": f"SWZ-{a.id}", "member": s.get(Member, a.member_id).name,
                                     "kes": a.amount_cents / 100, "outcome": a.appraisal_outcome,
                                     "waiting_days": round((now - (a.submitted_at or a.created_at)).total_seconds() / 86400, 1)})

    for g in s.scalars(select(LendingGroup).where(LendingGroup.institution_id == institution_id,
                                                  LendingGroup.status == "active", LendingGroup.officer_user_id == who.id,
                                                  LendingGroup.meeting_day == today.weekday())):
        n = s.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == g.id,
                                                                         GroupMember.left_on.is_(None)))
        out["groups_today"].append({"id": g.id, "group_no": g.group_no, "name": g.name, "members": n})

    if me:
        for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.status == "active",
                                               Loan.days_in_arrears.between(1, 7))):
            if mine(ln):
                m = s.get(Member, ln.member_id)
                out["newly_behind"].append({"loan_no": ln.loan_no, "member": m.name, "phone": m.phone,
                                            "days": ln.days_in_arrears, "arrears_kes": ln.arrears_cents / 100})

    if who.role in PERMISSIONS["recovery"]:
        from .recovery_api import _guarantor_rows, refund_due_cents

        for r in s.scalars(select(Recovery).where(Recovery.institution_id == institution_id)):
            due = refund_due_cents(r, _guarantor_rows(s, r))
            if due:
                out["refunds_owed"].append({"id": r.id, "loan_no": s.get(Loan, r.loan_id).loan_no, "kes": due / 100})
    return {"name": who.name, "today": today, **out}


FIELDS = [  # (model, attribute, label, what it switches on, applies to)
    ("member", "phone", "Phone", "SMS reminders, the member app, guarantor requests", None),
    ("member", "id_number", "ID number", "the guarantee network, duplicate checks, USSD consent", None),
    ("member", "joined_on", "Date joined", "the membership-period check in appraisal", None),
    ("member", "deposits_cents", "Deposits", "the deposits multiplier, guarantor capacity", None),
    ("member", "net_pay_cents", "Net pay", "the one-third rule (employed members only)", "employed"),
    ("member", "last_dividend_cents", "Last dividend", "dividend advances", None),
    ("loan", "officer", "Loan officer", "officer books and My day", None),
    ("loan", "next_due_on", "Next due date", "due-soon reminders in the member app", None),
    ("loan", "disbursed_on", "Disbursement date", "linking hand-overs to the core's loans", None),
]


@router.get("/institutions/{institution_id}/data-quality", dependencies=[Depends(require("read"))])
def data_quality(institution_id: int, s: Session = Depends(get_session)):
    members = list(s.scalars(select(Member).where(Member.institution_id == institution_id)))
    loans = list(s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.status == "active")))
    rows = []
    for kind, attr, label, use, only in FIELDS:
        pool = members if kind == "member" else loans
        if only == "employed":
            pool = [m for m in pool if m.employer]
        missing = sum(1 for x in pool if getattr(x, attr) in (None, ""))
        rows.append({"record": "members" if kind == "member" else "active loans", "field": label, "of": len(pool),
                     "missing": missing, "complete_pct": round(100 * (len(pool) - missing) / len(pool), 1) if pool else None,
                     "switches_on": use})
    return {"members": len(members), "active_loans": len(loans), "fields": rows}
