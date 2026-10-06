"""Collections: rank every loan in arrears and queue the right next action.

The ranking favours loans where action now recovers the most money: early
arrears (cheap to cure), large amounts, and check-off loans that silently
stopped (usually a payroll problem one phone call fixes).
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Institution, Loan, Member, Reminder


@dataclass
class Stage:
    name: str
    channel: str
    urgency: int


def stage_for(days: int) -> Stage:
    if days <= 7:
        return Stage("early", "sms", 30)
    if days <= 30:
        return Stage("first_month", "call", 40)
    if days <= 60:
        return Stage("watch", "guarantor_notice", 45)
    if days <= 90:
        return Stage("substandard", "field_visit", 50)
    if days <= 180:
        return Stage("doubtful", "recovery", 35)
    return Stage("loss", "recovery", 20)


def priority(loan: Loan) -> int:
    st = stage_for(loan.days_in_arrears)
    inst = max(loan.installment_cents, 1)
    amount_factor = min(loan.arrears_cents / inst, 6) / 6 * 35
    exposure = min(loan.balance_cents / 100_000_000, 1) * 15  # caps at KES 1M balance
    checkoff_stop = 10 if loan.repays_via == "checkoff" and 25 <= loan.days_in_arrears <= 60 else 0
    return int(round(min(100, st.urgency + amount_factor + exposure + checkoff_stop)))


def message(inst: Institution, m: Member, loan: Loan, st: Stage) -> str:
    first = m.name.split()[0].title()
    arrears = f"KES {loan.arrears_cents / 100:,.0f}"
    pay = f"Paybill {inst.paybill}, account {m.member_no}" if inst.paybill else f"account {m.member_no}"
    if loan.repays_via == "checkoff" and 25 <= loan.days_in_arrears <= 60:
        return (
            f"Dear {first}, your {loan.product} deduction of KES {loan.installment_cents / 100:,.0f} was not received from your employer. "
            f"Please confirm with payroll or pay via {pay}. {inst.name}"
        )
    if st.name == "early":
        return f"Dear {first}, a gentle reminder: {arrears} on loan {loan.loan_no} is now due. Pay via {pay}. Thank you. {inst.name}"
    if st.name == "first_month":
        return (
            f"Dear {first}, loan {loan.loan_no} is {loan.days_in_arrears} days overdue ({arrears}). "
            f"Please pay via {pay} or call us to agree a plan. {inst.name}"
        )
    if st.name == "watch":
        return (
            f"Dear {first}, loan {loan.loan_no} is {loan.days_in_arrears} days overdue ({arrears}). "
            f"Your guarantors will be notified unless it is paid or a plan is agreed. Pay via {pay}. {inst.name}"
        )
    if st.name == "substandard":
        return (
            f"FORMAL NOTICE: loan {loan.loan_no} is {loan.days_in_arrears} days overdue ({arrears}). "
            f"An officer will visit. Pay via {pay} or contact us immediately. {inst.name}"
        )
    return (
        f"Recovery action: loan {loan.loan_no}, {loan.days_in_arrears} days overdue, arrears {arrears}, "
        f"balance KES {loan.balance_cents / 100:,.0f}. Review guarantor recovery and deposit offset per policy."
    )


def build_queue(s: Session, institution_id: int) -> dict:
    inst = s.get(Institution, institution_id)
    s.query(Reminder).filter_by(institution_id=institution_id, status="queued").delete()
    loans = list(
        s.scalars(
            select(Loan).where(
                Loan.institution_id == institution_id,
                Loan.status == "active",
                Loan.arrears_cents > 0,
            )
        )
    )
    buckets: dict[str, dict] = {}
    for ln in loans:
        st = stage_for(ln.days_in_arrears)
        s.add(
            Reminder(
                institution_id=institution_id,
                loan_id=ln.id,
                channel=st.channel,
                message=message(inst, ln.member, ln, st),
                priority_score=priority(ln),
            )
        )
        b = buckets.setdefault(st.name, {"loans": 0, "arrears_kes": 0.0})
        b["loans"] += 1
        b["arrears_kes"] += ln.arrears_cents / 100
    s.commit()
    return {"queued": len(loans), "by_stage": buckets}


def portfolio_at_risk(s: Session, institution_id: int) -> dict:
    loans = list(s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.status == "active")))
    total = sum(ln.balance_cents for ln in loans) or 1
    def par(days):
        return round(100 * sum(ln.balance_cents for ln in loans if ln.days_in_arrears > days) / total, 2)
    return {
        "active_loans": len(loans),
        "portfolio_kes": total / 100,
        "arrears_kes": sum(ln.arrears_cents for ln in loans) / 100,
        "par_1": par(0),
        "par_30": par(30),
        "par_90": par(90),
    }
