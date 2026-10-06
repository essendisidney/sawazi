"""How a matched payment is split, per institution.

Sawazi never calculates interest or penalties (that is the core banking system's job). It takes the
penalty / interest / principal arrears from the core system's loans export and applies the order the
institution chooses. The defaults reproduce Sawazi's original behaviour exactly:
most overdue loan first, arrears, then the current instalment, then everything left to deposits.

The split is planned as a pure function (`plan`) so staff can preview rules without touching data,
and the same code allocates real payments (`apply`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Allocation, AllocationRules, Loan, Transaction

LOAN_ORDERS = {
    "most_overdue_first": "Most overdue loan first (then oldest)",
    "oldest_loan_first": "Oldest loan first",
    "largest_arrears_first": "Largest arrears first",
}
ARREARS_PARTS = ("penalty", "interest", "principal")
EXCESS_TARGETS = ("deposits", "shares")


@dataclass
class ExcessBucket:
    target: str
    percent: int | None = None  # share of what is left after loans
    max_cents: int | None = None  # fixed cap per payment
    # neither: takes everything that is left (must be the last bucket)


@dataclass
class Rules:
    loan_order: str = "most_overdue_first"
    arrears_order: list[str] = field(default_factory=lambda: list(ARREARS_PARTS))
    pay_current_installment: bool = True
    excess: list[ExcessBucket] = field(default_factory=lambda: [ExcessBucket("deposits")])

    def validate(self) -> None:
        if self.loan_order not in LOAN_ORDERS:
            raise ValueError(f"loan_order must be one of {', '.join(LOAN_ORDERS)}")
        if sorted(self.arrears_order) != sorted(ARREARS_PARTS):
            raise ValueError("arrears_order must list penalty, interest and principal, each once")
        if not self.excess:
            raise ValueError("excess needs at least one bucket")
        targets = [b.target for b in self.excess]
        if len(set(targets)) != len(targets) or any(t not in EXCESS_TARGETS for t in targets):
            raise ValueError(f"excess targets must be distinct, from {', '.join(EXCESS_TARGETS)}")
        *capped, last = self.excess
        if last.percent is not None or last.max_cents is not None:
            raise ValueError("the last excess bucket takes whatever is left: give it no percent or cap")
        for b in capped:
            if (b.percent is None) == (b.max_cents is None):
                raise ValueError(f"'{b.target}' needs either a percent or a cap (only the last bucket has neither)")
            if b.percent is not None and not 1 <= b.percent <= 99:
                raise ValueError("percent must be between 1 and 99")
            if b.max_cents is not None and b.max_cents <= 0:
                raise ValueError("cap must be more than zero")
        if sum(b.percent or 0 for b in capped) >= 100:
            raise ValueError("percentages must leave something for the last bucket")

    def as_dict(self) -> dict:
        return {
            "loan_order": self.loan_order,
            "arrears_order": list(self.arrears_order),
            "pay_current_installment": self.pay_current_installment,
            "excess": [{"target": b.target, "percent": b.percent,
                        "max_kes": b.max_cents / 100 if b.max_cents is not None else None} for b in self.excess],
        }


def rules_for(s: Session, institution_id: int) -> Rules:
    row = s.scalar(select(AllocationRules).where(AllocationRules.institution_id == institution_id))
    if not row:
        return Rules()
    return Rules(loan_order=row.loan_order, arrears_order=list(row.arrears_order),
                 pay_current_installment=row.pay_current_installment,
                 excess=[ExcessBucket(b["target"], b.get("percent"), b.get("max_cents")) for b in row.excess])


# ---------------------------------------------------------------- planning (pure)

@dataclass
class LoanState:
    id: int
    loan_no: str
    balance_cents: int
    installment_cents: int
    arrears_cents: int
    penalty_arrears_cents: int
    interest_arrears_cents: int
    days_in_arrears: int
    disbursed_key: str  # ISO date or "" (sorting)
    has_breakdown: bool = False  # the core export gave penalty/interest; the rest of arrears is principal

    @classmethod
    def of(cls, ln: Loan) -> LoanState:
        return cls(ln.id, ln.loan_no, ln.balance_cents, ln.installment_cents, ln.arrears_cents,
                   ln.penalty_arrears_cents or 0, ln.interest_arrears_cents or 0, ln.days_in_arrears,
                   ln.disbursed_on.isoformat() if ln.disbursed_on else "", bool(ln.arrears_breakdown))


@dataclass
class Line:
    target: str  # loan_penalty | loan_interest | loan_principal | loan_arrears | loan_installment | deposits | shares
    loan_id: int | None
    amount_cents: int


def order_loans(loans: list[LoanState], rules: Rules, preferred_loan_id: int | None = None) -> list[LoanState]:
    if rules.loan_order == "oldest_loan_first":
        key = lambda ln: (ln.disbursed_key or "9999", ln.id)  # noqa: E731
    elif rules.loan_order == "largest_arrears_first":
        key = lambda ln: (-ln.arrears_cents, -ln.days_in_arrears, ln.disbursed_key)  # noqa: E731
    else:
        key = lambda ln: (-ln.days_in_arrears, ln.disbursed_key)  # noqa: E731
    out = sorted(loans, key=key)
    if preferred_loan_id:  # the payment named a specific loan: serve it first
        out.sort(key=lambda ln: ln.id != preferred_loan_id)
    return out


def plan(amount_cents: int, loans: list[LoanState], rules: Rules, preferred_loan_id: int | None = None) -> list[Line]:
    """Split `amount_cents`. Mutates the LoanState copies to reflect what was paid. Every cent is placed."""
    remaining = amount_cents
    out: list[Line] = []

    def take(target: str, loan: LoanState | None, amount: int) -> int:
        nonlocal remaining
        amount = min(amount, remaining)
        if amount > 0:
            out.append(Line(target, loan.id if loan else None, amount))
            remaining -= amount
        return max(amount, 0)

    ordered = order_loans(loans, rules, preferred_loan_id)
    for ln in ordered:
        due = min(ln.arrears_cents, ln.balance_cents)
        if due <= 0:
            continue
        if ln.has_breakdown:
            principal = max(ln.arrears_cents - ln.penalty_arrears_cents - ln.interest_arrears_cents, 0)
            parts = {"penalty": ln.penalty_arrears_cents, "interest": ln.interest_arrears_cents, "principal": principal}
            for part in rules.arrears_order:
                paid = take(f"loan_{part}", ln, min(parts[part], due))
                due -= paid
                ln.arrears_cents -= paid
                ln.balance_cents -= paid
                if part == "penalty":
                    ln.penalty_arrears_cents -= paid
                elif part == "interest":
                    ln.interest_arrears_cents -= paid
        else:
            paid = take("loan_arrears", ln, due)
            ln.arrears_cents -= paid
            ln.balance_cents -= paid
        if ln.arrears_cents == 0:
            ln.days_in_arrears = 0
    if rules.pay_current_installment:
        for ln in ordered:
            ln.balance_cents -= take("loan_installment", ln, min(ln.installment_cents, ln.balance_cents))

    excess = remaining
    for b in rules.excess[:-1]:
        cap = excess * b.percent // 100 if b.percent is not None else b.max_cents
        take(b.target, None, cap)
    take(rules.excess[-1].target, None, remaining)
    assert remaining == 0 and sum(x.amount_cents for x in out) == amount_cents
    return out


# ---------------------------------------------------------------- applying to real payments

def apply(s: Session, t: Transaction, member_id: int, preferred_loan_id: int | None = None,
          rules: Rules | None = None) -> list[Allocation]:
    rules = rules or rules_for(s, t.institution_id)
    loans = list(s.scalars(select(Loan).where(Loan.institution_id == t.institution_id,
                                              Loan.member_id == member_id, Loan.status == "active")))
    states = {ln.id: LoanState.of(ln) for ln in loans}
    lines = plan(t.amount_cents, list(states.values()), rules, preferred_loan_id)
    for ln in loans:
        st = states[ln.id]
        ln.balance_cents, ln.arrears_cents = st.balance_cents, st.arrears_cents
        ln.penalty_arrears_cents, ln.interest_arrears_cents = st.penalty_arrears_cents, st.interest_arrears_cents
        ln.days_in_arrears = st.days_in_arrears
        if ln.balance_cents <= 0:
            ln.status = "closed"
    out = [Allocation(institution_id=t.institution_id, transaction_id=t.id, target=x.target, loan_id=x.loan_id,
                      amount_cents=x.amount_cents) for x in lines]
    s.add_all(out)
    t.member_id = member_id
    t.status = "allocated"
    return out
