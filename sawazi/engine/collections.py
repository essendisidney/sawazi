"""Collections: rank every loan in arrears and queue the right next action.

The ranking favours loans where action now recovers the most money: early
arrears (cheap to cure), large amounts, and check-off loans that silently
stopped (usually a payroll problem one phone call fixes).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

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


GROUP_MEETING = range(8, 91)  # days behind: past a gentle reminder, before recovery


def group_note(group, officer: str | None, m: Member, loan: Loan, own_loan: bool = False) -> str:
    """For the loan officer, not sent to anyone: in joint-liability lending the group is the guarantor, and its
    meeting is where a member's arrears are dealt with."""
    from ..groups_api import DAYS

    when = DAYS[group.meeting_day] if group.meeting_day is not None else "next"
    if own_loan:
        return (f"Raise at {group.name}'s {when} meeting ({group.group_no}): the group's own loan {loan.loan_no} is "
                f"KES {loan.arrears_cents / 100:,.0f} behind, {loan.days_in_arrears} days. Every member is liable for it."
                + (f" Officer: {officer}." if officer else ""))
    return (f"Raise at {group.name}'s {when} meeting ({group.group_no}): {m.name.title()} ({m.member_no}) is "
            f"KES {loan.arrears_cents / 100:,.0f} behind on loan {loan.loan_no}, {loan.days_in_arrears} days. "
            f"The group stands behind this loan." + (f" Officer: {officer}." if officer else ""))


def build_queue(s: Session, institution_id: int) -> dict:
    from ..models import GroupMember, LendingGroup, StaffUser

    inst = s.get(Institution, institution_id)
    in_group = {gm.member_id: (g, gm.role) for gm, g in s.execute(
        select(GroupMember, LendingGroup).join(LendingGroup, GroupMember.group_id == LendingGroup.id).where(
            GroupMember.institution_id == institution_id, GroupMember.left_on.is_(None),
            LendingGroup.status == "active"))}
    from ..oversight import open_promise_loans

    promised = open_promise_loans(s, institution_id, date.today())
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
    waiting = [ln for ln in loans if ln.id in promised]  # a promise is running: leave them be until its date
    loans = [ln for ln in loans if ln.id not in promised]
    for ln in loans:
        st = stage_for(ln.days_in_arrears)
        group, role = in_group.get(ln.member_id, (None, None))
        if group and (ln.days_in_arrears in GROUP_MEETING or role == "group_account"):
            officer = s.get(StaffUser, group.officer_user_id).name if group.officer_user_id else None
            channel = "group_meeting"
            text = group_note(group, officer, ln.member, ln, own_loan=role == "group_account")
        else:
            channel, text = st.channel, message(inst, ln.member, ln, st)
        s.add(
            Reminder(
                institution_id=institution_id,
                loan_id=ln.id,
                channel=channel,
                message=text,
                priority_score=priority(ln),
            )
        )
        b = buckets.setdefault(st.name, {"loans": 0, "arrears_kes": 0.0})
        b["loans"] += 1
        b["arrears_kes"] += ln.arrears_cents / 100
    s.commit()
    return {"queued": len(loans), "by_stage": buckets, "waiting_on_promises": len(waiting)}


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
