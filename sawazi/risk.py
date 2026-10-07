"""Builds the risk report from the database: every live pledge, every active loan, every member."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import guarantors
from .engine import exposure
from .models import CoreGuarantee, Guarantee, Loan, LoanApplication, Member


def pledges(s: Session, institution_id: int) -> list[exposure.Pledge]:
    """Accepted in Sawazi (on a loan, or on an application still being decided) and active in the core system
    (unless Sawazi recorded the same one)."""
    out = [exposure.Pledge(g.guarantor_member_id, a.member_id, a.disbursed_loan_id, g.amount_cents, "sawazi")
           for g, a in s.execute(select(Guarantee, LoanApplication)
                                 .join(LoanApplication, Guarantee.application_id == LoanApplication.id)
                                 .where(Guarantee.institution_id == institution_id, Guarantee.status == "accepted"))]
    out += [exposure.Pledge(cg.guarantor_member_id, ln.member_id, ln.id, cg.amount_cents, "core")
            for cg, ln in s.execute(select(CoreGuarantee, Loan).join(Loan, CoreGuarantee.loan_id == Loan.id)
                                    .where(CoreGuarantee.institution_id == institution_id,
                                           CoreGuarantee.status == "active", guarantors.core_counts()))]
    return out


def report(s: Session, institution_id: int) -> exposure.Report:
    members = [exposure.MemberRow(m.id, m.member_no, m.name, m.deposits_cents, m.employer)
               for m in s.scalars(select(Member).where(Member.institution_id == institution_id))]
    loans = [exposure.LoanRow(ln.id, ln.loan_no, ln.member_id, ln.product, ln.balance_cents, ln.arrears_cents,
                              ln.days_in_arrears)
             for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.status == "active"))]
    return exposure.report(members, loans, pledges(s, institution_id))
