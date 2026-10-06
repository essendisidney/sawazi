"""Check-off reconciliation: what the SACCO asked the employer to deduct versus what arrived.

For each employer and payroll period it finds:
  - short remittances (member deducted less than scheduled)
  - over remittances
  - members missing from the remittance entirely (often: left employment, payroll error)
  - members remitted but not on the schedule
  - payroll lines nobody can identify
Matched lines become transactions and flow through normal allocation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..importers.common import norm_name, norm_ref
from ..models import CheckoffRemittance, CheckoffSchedule, ExceptionItem, Member, Transaction


def reconcile_checkoff(s: Session, institution_id: int, employer: str, period: str) -> dict:
    schedule = {
        c.member_id: c.expected_cents
        for c in s.scalars(
            select(CheckoffSchedule).where(
                CheckoffSchedule.institution_id == institution_id,
                CheckoffSchedule.employer == employer,
                CheckoffSchedule.period == period,
            )
        )
    }
    lines = list(
        s.scalars(
            select(CheckoffRemittance).where(
                CheckoffRemittance.institution_id == institution_id,
                CheckoffRemittance.employer == employer,
                CheckoffRemittance.period == period,
            )
        )
    )
    members = {m.id: m for m in s.scalars(select(Member).where(Member.institution_id == institution_id))}
    by_no = {norm_ref(m.member_no): m.id for m in members.values()}
    # Payroll names are matched only against people on this employer's schedule.
    scheduled_names = {mid: norm_name(members[mid].name) for mid in schedule}

    # Clear exceptions from an earlier run of the same reconciliation.
    tag = f"[{employer} {period}]"
    s.query(ExceptionItem).filter(
        ExceptionItem.institution_id == institution_id,
        ExceptionItem.kind.like("checkoff_%"),
        ExceptionItem.detail.like(f"{tag}%"),
        ExceptionItem.status == "open",
    ).delete(synchronize_session=False)

    remitted = defaultdict(int)
    unidentified = []
    for ln in lines:
        mid = by_no.get(norm_ref(ln.member_no)) if ln.member_no else None
        if mid is None and ln.payroll_name and scheduled_names:
            hit = process.extractOne(norm_name(ln.payroll_name), scheduled_names, scorer=fuzz.token_set_ratio)
            if hit and hit[1] >= 90:
                mid = hit[2]
        ln.member_id = mid
        if mid is None:
            unidentified.append(ln)
        else:
            remitted[mid] += ln.remitted_cents

    def exc(kind, sev, member_id, amount, detail):
        s.add(
            ExceptionItem(
                institution_id=institution_id,
                kind=kind,
                severity=sev,
                member_id=member_id,
                amount_cents=amount,
                detail=f"{tag} {detail}",
            )
        )

    counts = defaultdict(int)
    for mid, expected in schedule.items():
        got = remitted.get(mid, 0)
        m = members[mid]
        if got == 0:
            counts["missing"] += 1
            exc(
                "checkoff_missing", "high", mid, expected,
                f"{m.member_no} {m.name}: nothing remitted, KES {expected / 100:,.0f} expected. "
                "Check if the member left, went on unpaid leave, or was dropped from payroll.",
            )
        elif got < expected:
            counts["short"] += 1
            exc(
                "checkoff_short", "medium", mid, expected - got,
                f"{m.member_no} {m.name}: KES {got / 100:,.0f} remitted against KES {expected / 100:,.0f} "
                f"(short by KES {(expected - got) / 100:,.0f}). Often a payroll one-third rule cap.",
            )
        elif got > expected:
            counts["over"] += 1
            exc(
                "checkoff_over", "low", mid, got - expected,
                f"{m.member_no} {m.name}: KES {got / 100:,.0f} remitted against KES {expected / 100:,.0f} "
                f"(over by KES {(got - expected) / 100:,.0f}). Excess goes to deposits unless instructed.",
            )
        else:
            counts["exact"] += 1
    for mid, got in remitted.items():
        if mid not in schedule:
            m = members[mid]
            counts["not_on_schedule"] += 1
            exc(
                "checkoff_unscheduled", "low", mid, got,
                f"{m.member_no} {m.name}: KES {got / 100:,.0f} remitted but not on this period's schedule.",
            )
    for ln in unidentified:
        counts["unidentified"] += 1
        exc(
            "checkoff_unidentified", "high", None, ln.remitted_cents,
            f"Payroll line '{ln.payroll_name or ''}' (no. '{ln.member_no or ''}') for KES {ln.remitted_cents / 100:,.0f} "
            "matches no member. Ask the employer's payroll office.",
        )

    # Turn identified remittances into transactions so allocation is uniform.
    when = datetime.strptime(period + "-28", "%Y-%m-%d")
    existing = set(
        s.scalars(
            select(Transaction.reference).where(
                Transaction.institution_id == institution_id, Transaction.source == "checkoff"
            )
        )
    )
    created = 0
    for mid, got in remitted.items():
        ref = f"CO-{norm_ref(employer)[:12]}-{period}-{members[mid].member_no}"
        if ref in existing or got <= 0:
            continue
        s.add(
            Transaction(
                institution_id=institution_id,
                source="checkoff",
                reference=ref,
                txn_time=when,
                amount_cents=got,
                payer_name=employer,
                account_ref=members[mid].member_no,
                narrative=f"Check-off {employer} {period}",
            )
        )
        created += 1
    s.commit()

    expected_total = sum(schedule.values())
    remitted_total = sum(ln.remitted_cents for ln in lines)
    return {
        "employer": employer,
        "period": period,
        "expected_kes": expected_total / 100,
        "remitted_kes": remitted_total / 100,
        "variance_kes": (remitted_total - expected_total) / 100,
        "collection_rate_pct": round(100 * remitted_total / expected_total, 1) if expected_total else None,
        "members_scheduled": len(schedule),
        "transactions_created": created,
        **counts,
    }
