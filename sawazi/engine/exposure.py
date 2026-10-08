"""Exposure and risk: where the portfolio and the guarantor network are weak, found early.

Pure functions over plain data. The report covers loan classification and provisioning, portfolio at risk by product
and employer, borrower concentration, and the guarantor network: who guarantees whom, how much, and the patterns
that usually come before losses (guarantors who are themselves behind, members guaranteeing each other, one member
backing too many loans, pledges above deposits, defaulted loans whose guarantors are also behind).

Flags are prompts for a person to look, never conclusions: a mutual guarantee can be innocent.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

# Loan classification and provisioning as in SASRA's risk classification of assets for deposit-taking SACCOs
# (days in arrears, provision rate in basis points). Verify against the current SASRA form before filing returns.
CLASSES = [
    ("performing", 0, 0, 100),
    ("watch", 1, 30, 500),
    ("substandard", 31, 180, 2500),
    ("doubtful", 181, 360, 5000),
    ("loss", 361, None, 10000),
]


@dataclass
class Limits:
    arrears_days: int = 30  # "behind" for flags and PAR 30
    many_guarantees: int = 5  # one member backing this many loans or more
    borrower_share_bps: int = 500  # one borrower holding more than 5% of the portfolio...
    concentration_min_loans: int = 20  # ...but only in a book big enough for a share to mean something


@dataclass
class MemberRow:
    id: int
    member_no: str
    name: str
    deposits_cents: int | None
    employer: str | None


@dataclass
class LoanRow:
    id: int
    loan_no: str
    member_id: int
    product: str
    balance_cents: int
    arrears_cents: int
    days_in_arrears: int


@dataclass
class Pledge:
    guarantor_id: int
    borrower_id: int
    loan_id: int | None  # None while the application is still being decided
    amount_cents: int
    source: str  # sawazi | core


@dataclass
class Flag:
    kind: str
    severity: str  # high | medium
    member_nos: list[str]
    amount_cents: int
    message: str


@dataclass
class Report:
    portfolio: dict
    classification: list[dict]
    par_by_product: list[dict]
    par_by_employer: list[dict]
    concentration: dict
    guarantors: dict
    flags: list[Flag] = field(default_factory=list)


def kes(cents: int) -> str:
    return f"KES {cents / 100:,.0f}"


def classify(days: int) -> str:
    for name, lo, hi, _ in CLASSES:
        if days >= lo and (hi is None or days <= hi):
            return name
    return "loss"


def _par_rows(groups: dict[str, list[LoanRow]], limits: Limits) -> list[dict]:
    rows = []
    for key, loans in groups.items():
        bal = sum(ln.balance_cents for ln in loans)
        risk = sum(ln.balance_cents for ln in loans if ln.days_in_arrears > limits.arrears_days)
        rows.append({"name": key, "loans": len(loans), "balance_cents": bal, "at_risk_cents": risk,
                     "par_bps": risk * 10000 // bal if bal else 0})
    return sorted(rows, key=lambda r: (-r["at_risk_cents"], -r["balance_cents"]))


def report(members: list[MemberRow], loans: list[LoanRow], pledges: list[Pledge], limits: Limits | None = None) -> Report:
    limits = limits or Limits()
    by_id = {m.id: m for m in members}
    no = lambda mid: by_id[mid].member_no if mid in by_id else "?"  # noqa: E731
    total = sum(ln.balance_cents for ln in loans)

    # ---- portfolio and classification
    def par(days):
        return sum(ln.balance_cents for ln in loans if ln.days_in_arrears > days)
    portfolio = {"loans": len(loans), "balance_cents": total, "arrears_cents": sum(ln.arrears_cents for ln in loans),
                 "par1_bps": par(0) * 10000 // total if total else 0,
                 "par30_bps": par(30) * 10000 // total if total else 0,
                 "par90_bps": par(90) * 10000 // total if total else 0}
    classification = []
    for name, lo, hi, rate in CLASSES:
        group = [ln for ln in loans if classify(ln.days_in_arrears) == name]
        bal = sum(ln.balance_cents for ln in group)
        classification.append({"class": name, "days": f"{lo}" if lo == 0 else f"{lo}+" if hi is None else f"{lo}-{hi}",
                               "loans": len(group), "balance_cents": bal, "provision_bps": rate,
                               "provision_cents": -(-bal * rate // 10000)})
    portfolio["provision_cents"] = sum(c["provision_cents"] for c in classification)

    by_product, by_employer = defaultdict(list), defaultdict(list)
    for ln in loans:
        by_product[ln.product or "Unnamed product"].append(ln)
        m = by_id.get(ln.member_id)
        by_employer[(m.employer if m and m.employer else "Not on check-off")].append(ln)

    # ---- concentration
    per_borrower = defaultdict(int)
    for ln in loans:
        per_borrower[ln.member_id] += ln.balance_cents
    top = sorted(per_borrower.items(), key=lambda kv: -kv[1])[:10]
    concentration = {"top10_cents": sum(v for _, v in top),
                     "top10_bps": sum(v for _, v in top) * 10000 // total if total else 0,
                     "top": [{"member_no": no(mid), "name": by_id[mid].name if mid in by_id else "?",
                              "balance_cents": v, "share_bps": v * 10000 // total if total else 0} for mid, v in top]}

    flags: list[Flag] = []
    for mid, v in top if len(loans) >= limits.concentration_min_loans else []:
        if total and v * 10000 // total > limits.borrower_share_bps:
            flags.append(Flag("concentration", "medium", [no(mid)], v,
                              f"{no(mid)} owes {kes(v)}, {v * 100 / total:.1f}% of the whole portfolio."))

    # ---- guarantor network
    worst_days = defaultdict(int)  # member -> worst arrears on their own loans
    for ln in loans:
        worst_days[ln.member_id] = max(worst_days[ln.member_id], ln.days_in_arrears)
    loans_by_id = {ln.id: ln for ln in loans}
    pledged = defaultdict(int)
    backs = defaultdict(set)  # guarantor -> borrowers
    loans_backed = defaultdict(set)  # guarantor -> loans (or pending applications)
    guarantors_of_loan = defaultdict(list)
    for p in pledges:
        pledged[p.guarantor_id] += p.amount_cents
        backs[p.guarantor_id].add(p.borrower_id)
        loans_backed[p.guarantor_id].add(p.loan_id if p.loan_id is not None else ("app", p.borrower_id))
        if p.loan_id is not None:
            guarantors_of_loan[p.loan_id].append(p)
    behind = limits.arrears_days

    for gid, amount in pledged.items():
        m = by_id.get(gid)
        if worst_days[gid] > behind:
            flags.append(Flag("guarantor_in_arrears", "high", [no(gid)], amount,
                              f"{no(gid)} guarantees {kes(amount)} for others but is {worst_days[gid]} days behind "
                              f"on their own loan."))
        if m and m.deposits_cents is not None and amount > m.deposits_cents:
            flags.append(Flag("over_pledged", "high", [no(gid)], amount - m.deposits_cents,
                              f"{no(gid)} guarantees {kes(amount)} with deposits of {kes(m.deposits_cents)}."))
        if len(loans_backed[gid]) >= limits.many_guarantees:
            flags.append(Flag("many_guarantees", "medium", [no(gid)], amount,
                              f"{no(gid)} guarantees {len(loans_backed[gid])} loans, {kes(amount)} in all."))

    seen_cycles = set()
    for a in list(backs):
        for b in backs[a]:
            if a in backs.get(b, ()):
                cyc = tuple(sorted((a, b)))
                if cyc not in seen_cycles:
                    seen_cycles.add(cyc)
                    x, y = sorted((no(a), no(b)))
                    flags.append(Flag("mutual_guarantee", "medium", [x, y], 0,
                                      f"{x} and {y} guarantee each other's loans."))
            for c in backs.get(b, ()):
                if c not in (a, b) and a in backs.get(c, ()):
                    cyc = tuple(sorted((a, b, c)))
                    if cyc not in seen_cycles:
                        seen_cycles.add(cyc)
                        names = sorted(no(x) for x in cyc)
                        flags.append(Flag("guarantee_circle", "medium", names, 0,
                                          f"{', '.join(names)} guarantee each other in a circle."))

    at_risk_guaranteed = 0
    for loan_id, ps in guarantors_of_loan.items():
        ln = loans_by_id.get(loan_id)
        if ln is None or ln.days_in_arrears <= behind:
            continue
        at_risk_guaranteed += sum(p.amount_cents for p in ps)
        weak = [p for p in ps if worst_days[p.guarantor_id] > behind]
        if weak:
            flags.append(Flag("chain_default", "high", [no(ln.member_id)] + [no(p.guarantor_id) for p in weak],
                              ln.balance_cents,
                              f"Loan {ln.loan_no} is {ln.days_in_arrears} days behind and "
                              f"{len(weak)} of its {len(ps)} guarantor(s) are behind too: recovery from them is doubtful."))

    guarantors = {"members_guaranteeing": len(pledged), "pledged_cents": sum(pledged.values()),
                  "on_loans_behind_cents": at_risk_guaranteed,
                  "top": [{"member_no": no(g), "name": by_id[g].name if g in by_id else "?", "pledged_cents": v,
                           "loans": len(loans_backed[g]),
                           "deposits_cents": by_id[g].deposits_cents if g in by_id else None}
                          for g, v in sorted(pledged.items(), key=lambda kv: -kv[1])[:10]]}
    sev = {"high": 0, "medium": 1}
    flags.sort(key=lambda f: (sev[f.severity], -f.amount_cents))
    return Report(portfolio, classification, _par_rows(by_product, limits), _par_rows(by_employer, limits),
                  concentration, guarantors, flags)
