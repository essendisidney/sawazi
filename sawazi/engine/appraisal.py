"""Loan appraisal: does an application meet the product's rules, and why or why not.

Pure functions over plain data, so they are easy to test and the same code runs for a live application
and a "what if" check. Appraisal never approves anything: it gives a person every check with a plain
reason, and the largest amount the member qualifies for. A person decides.

Unknown is never treated as a pass. If Sawazi does not have the figure a rule needs (no join date,
no deposits, no payslip), that check is "unknown" and the application is incomplete, not eligible.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

PASS, FAIL, WARN, UNKNOWN, PENDING = "pass", "fail", "warn", "unknown", "pending"
INTEREST_METHODS = ("reducing", "flat")
GUARANTOR_COVER = ("above_deposits", "full", "none")


@dataclass
class Product:
    min_amount_cents: int
    max_amount_cents: int
    max_term_months: int
    interest_rate_bps: int
    interest_method: str = "reducing"
    deposits_multiplier_pct: int = 300
    min_membership_months: int = 6
    max_arrears_days: int = 30
    one_third_rule: bool = True
    guarantor_cover: str = "above_deposits"
    min_guarantors: int = 0


@dataclass
class MemberFacts:
    joined_on: date | None
    deposits_cents: int | None
    gross_pay_cents: int | None
    net_pay_cents: int | None


@dataclass
class ExistingLoan:
    loan_no: str
    balance_cents: int
    days_in_arrears: int


@dataclass
class Guarantee:
    name: str
    amount_cents: int
    status: str  # requested | accepted | declined | expired


@dataclass
class Check:
    code: str
    status: str
    message: str


@dataclass
class Appraisal:
    outcome: str  # passes | fails | incomplete
    checks: list[Check]
    instalment_cents: int
    max_eligible_cents: int  # the lowest of the limits that are known
    max_eligible_partial: bool  # True when a limit could not be worked out (e.g. no payslip): it may be lower
    required_cover_cents: int
    accepted_cover_cents: int
    limits: dict = field(default_factory=dict)


def kes(cents: int) -> str:
    return f"KES {cents / 100:,.0f}" if cents % 100 == 0 else f"KES {cents / 100:,.2f}"


def months_between(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    return months - (1 if end.day < start.day else 0)


# ---------------------------------------------------------------- instalment (estimate only)

def instalment(principal_cents: int, term_months: int, rate_bps: int, method: str) -> int:
    """Monthly instalment estimate, rounded up to the cent. The core system's schedule is the real one."""
    p, n = Decimal(principal_cents), Decimal(term_months)
    r = Decimal(rate_bps) / Decimal(120000)  # monthly rate
    if method == "flat":
        amount = p / n + p * r
    elif r == 0:
        amount = p / n
    else:
        amount = p * r / (1 - (1 + r) ** -term_months)
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def principal_for(instalment_cents: int, term_months: int, rate_bps: int, method: str) -> int:
    """Largest principal whose instalment fits within `instalment_cents` (inverse of `instalment`)."""
    if instalment_cents <= 0:
        return 0
    i, n = Decimal(instalment_cents), Decimal(term_months)
    r = Decimal(rate_bps) / Decimal(120000)
    if method == "flat":
        p = i / (1 / n + r)
    elif r == 0:
        p = i * n
    else:
        p = i * (1 - (1 + r) ** -term_months) / r
    p = int(p.to_integral_value(rounding=ROUND_FLOOR))
    while p > 0 and instalment(p, term_months, rate_bps, method) > instalment_cents:
        p -= 1  # rounding can push the instalment a cent over
    return p


# ---------------------------------------------------------------- the appraisal

def appraise(amount_cents: int, term_months: int, product: Product, member: MemberFacts,
             other_loans: list[ExistingLoan], guarantees: list[Guarantee], on: date) -> Appraisal:
    checks: list[Check] = []
    limits: dict[str, int] = {"product_max": product.max_amount_cents}
    inst = instalment(amount_cents, term_months, product.interest_rate_bps, product.interest_method)

    # 1. Product limits
    if amount_cents < product.min_amount_cents or amount_cents > product.max_amount_cents:
        checks.append(Check("amount", FAIL, f"{kes(amount_cents)} is outside this product's range of "
                                            f"{kes(product.min_amount_cents)} to {kes(product.max_amount_cents)}."))
    else:
        checks.append(Check("amount", PASS, f"{kes(amount_cents)} is within the product's range."))
    if not 1 <= term_months <= product.max_term_months:
        checks.append(Check("term", FAIL, f"{term_months} months is longer than this product allows "
                                          f"({product.max_term_months} months)."))
    else:
        checks.append(Check("term", PASS, f"{term_months} months is within the product's maximum."))

    # 2. Membership period
    if product.min_membership_months > 0:
        if member.joined_on is None:
            checks.append(Check("membership", UNKNOWN, "Join date not known: upload a members export with it."))
        else:
            months = months_between(member.joined_on, on)
            ok = months >= product.min_membership_months
            checks.append(Check("membership", PASS if ok else FAIL,
                                f"Member for {months} months; the product needs {product.min_membership_months}."))

    # 3. Existing loans in arrears
    behind = sorted(other_loans, key=lambda ln: -ln.days_in_arrears)
    worst = behind[0] if behind and behind[0].days_in_arrears > 0 else None
    if worst is None:
        checks.append(Check("arrears", PASS, "No existing loan is in arrears."))
    elif worst.days_in_arrears > product.max_arrears_days:
        checks.append(Check("arrears", FAIL, f"Loan {worst.loan_no} is {worst.days_in_arrears} days in arrears "
                                             f"(the limit is {product.max_arrears_days})."))
    else:
        checks.append(Check("arrears", WARN, f"Loan {worst.loan_no} is {worst.days_in_arrears} days in arrears."))

    # 4. Deposits multiplier: all loans together, including this one, against deposits
    outstanding = sum(ln.balance_cents for ln in other_loans)
    if product.deposits_multiplier_pct > 0:
        mult = f"{product.deposits_multiplier_pct / 100:g}x"
        if member.deposits_cents is None:
            checks.append(Check("deposits", UNKNOWN, "Deposits not known: upload member balances."))
            limits["deposits"] = None
        else:
            cap = member.deposits_cents * product.deposits_multiplier_pct // 100
            limits["deposits"] = max(cap - outstanding, 0)
            ok = amount_cents + outstanding <= cap
            checks.append(Check("deposits", PASS if ok else FAIL,
                                f"{mult} deposits of {kes(member.deposits_cents)} allows {kes(cap)} in loans; "
                                f"with {kes(outstanding)} already outstanding this loan can be up to "
                                f"{kes(limits['deposits'])}."))

    # 5. One-third take-home rule (Employment Act s.19(3)): pay after deductions stays at least a third of gross
    if product.one_third_rule:
        if member.gross_pay_cents is None or member.net_pay_cents is None:
            checks.append(Check("one_third", UNKNOWN, "Not checked: no payslip figures for this member."))
            limits["affordability"] = None
        else:
            floor = -(-member.gross_pay_cents // 3)  # a third of gross, rounded up
            room = member.net_pay_cents - floor
            limits["affordability"] = principal_for(room, term_months, product.interest_rate_bps,
                                                    product.interest_method)
            left = member.net_pay_cents - inst
            ok = left >= floor
            checks.append(Check("one_third", PASS if ok else FAIL,
                                f"Take-home after the {kes(inst)} instalment would be {kes(left)}; "
                                f"it must stay at least {kes(floor)} (a third of {kes(member.gross_pay_cents)} gross)."))

    # 6. Guarantor cover
    if product.guarantor_cover == "none":
        required = 0
    elif product.guarantor_cover == "full":
        required = amount_cents
    else:
        required = max(amount_cents - (member.deposits_cents or 0), 0) if member.deposits_cents is not None else amount_cents
    accepted = [g for g in guarantees if g.status == "accepted"]
    waiting = [g for g in guarantees if g.status == "requested"]
    cover = sum(g.amount_cents for g in accepted)
    enough_people = len(accepted) >= product.min_guarantors
    if required == 0 and enough_people:
        checks.append(Check("guarantors", PASS, "No guarantor cover needed." if not product.min_guarantors
                            else f"{len(accepted)} guarantors accepted."))
    elif cover >= required and enough_people:
        checks.append(Check("guarantors", PASS, f"Guarantors cover {kes(cover)} of the {kes(required)} needed."))
    elif waiting:
        checks.append(Check("guarantors", PENDING, f"{kes(cover)} of {kes(required)} covered; waiting for "
                                                   f"{len(waiting)} guarantor{'s' if len(waiting) != 1 else ''} to answer."))
    else:
        short = []
        if cover < required:
            short.append(f"{kes(required - cover)} more cover")
        if not enough_people:
            short.append(f"{product.min_guarantors - len(accepted)} more guarantor(s)")
        checks.append(Check("guarantors", FAIL, f"Needs {' and '.join(short)}."))

    statuses = {c.status for c in checks}
    outcome = "fails" if FAIL in statuses else "incomplete" if statuses & {UNKNOWN, PENDING} else "passes"
    known = [v for v in limits.values() if v is not None]
    partial = any(v is None for v in limits.values())
    return Appraisal(outcome, checks, inst, min(known), partial, required, cover, limits)
