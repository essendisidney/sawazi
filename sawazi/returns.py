"""SASRA return working papers for deposit-taking SACCOs.

Form 4 (risk classification of assets and provisioning) is due quarterly, by the 15th of the month after the
quarter. This module produces a WORKING SCHEDULE for it from Sawazi's data, not the official form: staff use it
to fill SASRA's template, and every total can be traced to the loan-level list. Once the official template is in
the repository, it will be filled in its exact layout.

Classes and rates come from `engine.exposure.CLASSES`. They follow the published summaries of the 2010
regulations and must be confirmed against the current gazetted text before anything is filed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from .engine.exposure import CLASSES, classify
from .models import Loan, Member, PortfolioSnapshot

SUSPEND_INTEREST = ("substandard", "doubtful", "loss")  # interest on these must be suspended, not taken as income


@dataclass
class LoanLine:
    loan_no: str
    member_no: str
    member: str
    product: str
    principal_cents: int
    balance_cents: int
    interest_arrears_cents: int
    days_in_arrears: int
    loan_class: str
    rate_bps: int
    provision_cents: int


def quarter_of(d: date) -> tuple[str, date, date]:
    """("2026 Q3", quarter end, filing deadline: the 15th of the month after the quarter)."""
    q = (d.month - 1) // 3 + 1
    end_month = q * 3
    end = date(d.year + (end_month == 12), end_month % 12 + 1, 1)
    quarter_end = date.fromordinal(end.toordinal() - 1)
    return f"{d.year} Q{q}", quarter_end, date(end.year, end.month, 15)


def form4_schedule(s: Session, institution_id: int, today: date) -> dict:
    rates = {name: rate for name, _, _, rate in CLASSES}
    lines: list[LoanLine] = []
    for ln, m in s.execute(select(Loan, Member).join(Member, Loan.member_id == Member.id).where(
            Loan.institution_id == institution_id, Loan.status == "active").order_by(Loan.days_in_arrears.desc(),
                                                                                       Loan.loan_no)):
        cls = classify(ln.days_in_arrears)
        lines.append(LoanLine(ln.loan_no, m.member_no, m.name, ln.product, ln.principal_cents, ln.balance_cents,
                              (ln.interest_arrears_cents or 0) if ln.arrears_breakdown else 0, ln.days_in_arrears,
                              cls, rates[cls], -(-ln.balance_cents * rates[cls] // 10000)))
    classes = []
    for name, lo, hi, rate in CLASSES:
        group = [x for x in lines if x.loan_class == name]
        classes.append({"class": name, "days": f"{lo}" if lo == 0 else f"{lo}+" if hi is None else f"{lo}-{hi}",
                        "loans": len(group), "balance_cents": sum(x.balance_cents for x in group),
                        "provision_bps": rate, "provision_cents": sum(x.provision_cents for x in group),
                        "interest_arrears_cents": sum(x.interest_arrears_cents for x in group)})
    snap = s.scalar(select(PortfolioSnapshot).where(PortfolioSnapshot.institution_id == institution_id)
                    .order_by(PortfolioSnapshot.as_of.desc()).limit(1))
    as_of = snap.as_of if snap else today
    label, quarter_end, due = quarter_of(as_of)
    breakdown = s.scalar(select(Loan.id).where(Loan.institution_id == institution_id, Loan.status == "active",
                                               Loan.arrears_breakdown.is_(True)).limit(1)) is not None
    npl = [c for c in classes if c["class"] in SUSPEND_INTEREST]
    total_bal = sum(c["balance_cents"] for c in classes)
    return {
        "generated_on": today,  # the loans as they stand at download time
        "as_of": as_of,  # the latest snapshot (normally the last loans upload): decides the quarter
        "quarter": label, "quarter_end": quarter_end, "due": due,
        "is_quarter_end": as_of == quarter_end,
        "classes": classes,
        "totals": {"loans": len(lines), "balance_cents": total_bal,
                   "provision_cents": sum(c["provision_cents"] for c in classes),
                   "npl_balance_cents": sum(c["balance_cents"] for c in npl),
                   "npl_bps": sum(c["balance_cents"] for c in npl) * 10000 // total_bal if total_bal else 0,
                   "interest_to_suspend_cents": sum(c["interest_arrears_cents"] for c in npl)},
        "interest_known": breakdown,
        "lines": lines,
    }
