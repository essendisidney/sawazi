"""Payment matching and allocation.

Every money-in line is matched to a member using the strongest available
evidence, then split by the institution's allocation rules (engine/allocation.py).
Anything the engine is not confident about goes to suspense with a suggested
member and the reason, so a person can clear it in seconds instead of hours.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..importers.common import norm_name, norm_ref
from . import allocation
from ..models import Allocation, ExceptionItem, Loan, Member, Transaction

AUTO_ALLOCATE_AT = 85  # confidence needed to allocate without a human


@dataclass
class Candidate:
    member_id: int
    confidence: int
    method: str
    loan_id: int | None = None


class MemberIndex:
    """In-memory lookups for one institution, rebuilt per matching run."""

    def __init__(self, s: Session, institution_id: int):
        members = list(s.scalars(select(Member).where(Member.institution_id == institution_id)))
        self.by_id = {m.id: m for m in members}
        self.by_no = {norm_ref(m.member_no): m.id for m in members}
        self.by_idno = {norm_ref(m.id_number): m.id for m in members if m.id_number}
        self.by_phone: dict[str, list[int]] = defaultdict(list)
        for m in members:
            if m.phone:
                self.by_phone[m.phone].append(m.id)
        self.names = {m.id: norm_name(m.name) for m in members}
        loans = list(s.scalars(select(Loan).where(Loan.institution_id == institution_id)))
        self.loan_by_no = {norm_ref(ln.loan_no): ln for ln in loans}
        self.member_nos = list(self.by_no.keys())
        from ..models import LendingGroup
        self.group_nos = {norm_ref(g) for g in s.scalars(select(LendingGroup.group_no).where(
            LendingGroup.institution_id == institution_id))}

    def name_lookup(self, text: str) -> int | None:
        """Member whose every name appears in the text (allowing small spelling slips). Unique hits only."""
        words = set(norm_name(text).split())
        hits = []
        for mid, name in self.names.items():
            parts = name.split()
            if len(parts) >= 2 and all(
                p in words or any(len(w) > 3 and fuzz.ratio(p, w) >= 88 for w in words) for p in parts
            ):
                hits.append(mid)
        return hits[0] if len(hits) == 1 else None

    def name_score(self, member_id: int, name: str | None) -> int:
        if not name:
            return 0
        return int(fuzz.token_set_ratio(self.names.get(member_id, ""), norm_name(name)))


_DATES = re.compile(r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b")


def find_member(idx: MemberIndex, t: Transaction) -> tuple[Candidate | None, str]:
    """Return the best candidate and a human-readable reason."""
    ref = norm_ref(t.account_ref)
    phone_hits = idx.by_phone.get(t.payer_phone or "", [])

    # Bank narratives often carry the member or loan number inside free text. Dates are taken out first, so
    # "13/03/2026" can never be read as member 2026.
    narrative_tokens = []
    if t.narrative:
        text = _DATES.sub(" ", t.narrative)
        narrative_tokens = [norm_ref(x) for x in re.findall(r"[A-Za-z]{0,4}[-/ ]?\d{3,}", text)]

    def conflict(mid: int) -> tuple[int, str] | None:
        """A valid reference can still be a typo of the payer's own number.

        If the payer's phone belongs to another member whose number is one
        character away from the reference, the money most likely belongs to
        the payer, not the member the reference happens to point at.
        """
        for owner in phone_hits:
            if owner != mid and ref and Levenshtein.distance(norm_ref(idx.by_id[owner].member_no), ref) <= 1:
                return owner, (
                    f"reference points to {idx.by_id[mid].member_no} {idx.by_id[mid].name}, but it was paid from the "
                    f"registered phone of {idx.by_id[owner].member_no} {idx.by_id[owner].name}, whose number is one "
                    "character away. Likely a typo"
                )
        return None

    def lookup(tok: str):
        if tok in idx.by_no:
            hit = Candidate(idx.by_no[tok], 100, "member_no"), "account reference is the member number"
        elif tok in idx.loan_by_no:
            ln = idx.loan_by_no[tok]
            hit = Candidate(ln.member_id, 98, "loan_no", ln.id), "account reference is a loan number"
        elif tok in idx.by_idno:
            hit = Candidate(idx.by_idno[tok], 95, "id_number"), "account reference is the member's ID number"
        else:
            hit = None
        return hit

    if ref and ref in idx.group_nos:  # paid to the group's account: several members' money, never guessed
        return None, (f"paid to group {ref}'s account: match it to the group's meeting sheet as banked cash, "
                      "or split it across the members who paid")
    if ref:
        hit = lookup(ref)
        if hit:
            clash = conflict(hit[0].member_id)
            if clash:
                return Candidate(clash[0], 70, "ref_conflict"), clash[1]
            return hit

    # A number in the narrative is a clue, not proof: a cheque, branch or batch number can equal a member number.
    # It is trusted only if it carries letters (a member or loan number with its prefix) or the payer's name or
    # phone confirms it, and never when the narrative points at more than one member.
    found = {}
    for tok in narrative_tokens:
        hit = tok and lookup(tok)
        if hit:
            found.setdefault(hit[0].member_id, (tok, hit))
    if len(found) > 1:
        first = next(iter(found.values()))[1][0]
        names = ", ".join(idx.by_id[m].member_no for m in found)
        return Candidate(first.member_id, 60, "narrative_ambiguous"), f"the narrative mentions more than one member ({names})"
    if found:
        tok, (cand, reason) = next(iter(found.values()))
        mid = cand.member_id
        confirmed = (not tok.isdigit()) or mid in phone_hits or idx.name_score(mid, t.payer_name or t.narrative) >= 85
        if confirmed:
            return cand, reason.replace("account reference", "the narrative")
        return Candidate(mid, 80, "narrative_number"), (
            f"the narrative contains {tok}, which is {idx.by_id[mid].member_no} {idx.by_id[mid].name}, "
            "but no name or phone confirms it")

    # Typo in the member number (one character off). Only trust it with corroboration.
    if ref and len(ref) >= 4:
        close = [no for no in idx.member_nos if Levenshtein.distance(no, ref) == 1]
        if len(close) == 1:
            mid = idx.by_no[close[0]]
            corroborated = mid in phone_hits or idx.name_score(mid, t.payer_name) >= 85
            if corroborated:
                return Candidate(mid, 90, "member_no_typo+corroborated"), (
                    f"reference '{t.account_ref}' is one character off member {close[0]}, and the payer's phone or name matches"
                )
            return Candidate(mid, 60, "member_no_typo"), (
                f"reference '{t.account_ref}' is one character off member {close[0]}, but nothing else confirms it"
            )

    # Paid from the member's registered phone.
    if len(phone_hits) == 1:
        return Candidate(phone_hits[0], 90, "phone"), "paid from the member's registered phone number"
    if len(phone_hits) > 1:
        best = max(phone_hits, key=lambda m: idx.name_score(m, t.payer_name))
        return Candidate(best, 55, "shared_phone"), "phone number is registered to more than one member"

    # Last resort: payer name (or the name inside a bank narrative). Never auto-allocated.
    name_text = t.payer_name or t.narrative
    if name_text:
        mid = idx.name_lookup(name_text)
        if mid:
            return Candidate(mid, 65, "name"), f"payer name looks like {idx.by_id[mid].name}, but no number or phone confirms it"

    return None, "no member number, loan number, ID or registered phone found"


def allocate(s: Session, t: Transaction, member_id: int, preferred_loan_id: int | None = None,
             rules: allocation.Rules | None = None) -> list[Allocation]:
    """Split a payment using the institution's allocation rules (engine/allocation.py)."""
    return allocation.apply(s, t, member_id, preferred_loan_id, rules)


def run_matching(s: Session, institution_id: int) -> dict:
    idx = MemberIndex(s, institution_id)
    rules = allocation.rules_for(s, institution_id)
    pending = list(
        s.scalars(
            select(Transaction)
            .where(Transaction.institution_id == institution_id, Transaction.status == "unmatched")
            .order_by(Transaction.txn_time)
        )
    )
    summary = defaultdict(int)
    for t in pending:
        cand, reason = find_member(idx, t)
        if cand and cand.confidence >= AUTO_ALLOCATE_AT:
            t.match_method, t.match_confidence = cand.method, cand.confidence
            allocate(s, t, cand.member_id, cand.loan_id, rules)
            summary["allocated"] += 1
            summary["allocated_cents"] += t.amount_cents
        else:
            t.status = "suspense"
            if cand:
                t.member_id = cand.member_id  # suggestion only
                t.match_method, t.match_confidence = cand.method, cand.confidence
            suggestion = f" Suggested member: {idx.by_id[cand.member_id].member_no} {idx.by_id[cand.member_id].name}." if cand else ""
            s.add(
                ExceptionItem(
                    institution_id=institution_id,
                    kind="suspense",
                    severity="high" if t.amount_cents >= 5_000_000 else "medium",
                    transaction_id=t.id,
                    member_id=cand.member_id if cand else None,
                    amount_cents=t.amount_cents,
                    detail=f"{t.source.upper()} {t.reference} from {t.payer_name or 'unknown'}: {reason}.{suggestion}",
                )
            )
            summary["suspense"] += 1
            summary["suspense_cents"] += t.amount_cents
    s.flush()
    summary["anomalies"] = flag_anomalies(s, institution_id, pending, idx)
    # Loans these payments finished free their guarantors, in the same transaction, so still under the caller's
    # matching lock (on PostgreSQL the lock ends at this commit).
    from ..guarantors import release_repaid
    summary["guarantees_released"] = release_repaid(s, institution_id)
    s.commit()
    total = summary["allocated"] + summary["suspense"]
    summary["processed"] = total
    summary["auto_match_rate"] = round(100 * summary["allocated"] / total, 1) if total else 0.0
    return dict(summary)


def flag_anomalies(s: Session, institution_id: int, txns: list[Transaction], idx: MemberIndex) -> int:
    """Patterns worth a second look even when the money matched."""
    count = 0

    def flag(kind, sev, t, detail):
        nonlocal count
        s.add(
            ExceptionItem(
                institution_id=institution_id,
                kind=kind,
                severity=sev,
                transaction_id=t.id,
                member_id=t.member_id,
                amount_cents=t.amount_cents,
                detail=detail,
            )
        )
        count += 1

    # Same payer, same amount, minutes apart: possible double payment.
    by_payer = defaultdict(list)
    for t in txns:
        if t.payer_phone:
            by_payer[(t.payer_phone, t.amount_cents)].append(t)
    for group in by_payer.values():
        group.sort(key=lambda x: x.txn_time)
        for a, b in zip(group, group[1:]):
            if b.txn_time - a.txn_time <= timedelta(minutes=15):
                flag(
                    "possible_double_payment",
                    "low",
                    b,
                    f"{b.reference} repeats {a.reference}: same phone, same amount, "
                    f"{int((b.txn_time - a.txn_time).total_seconds() // 60)} min apart. Member may want a refund or it was intended for another account.",
                )

    # Paid for someone else: payer's phone belongs to a different member.
    for t in txns:
        if t.status == "allocated" and t.payer_phone:
            owners = idx.by_phone.get(t.payer_phone, [])
            if owners and t.member_id not in owners:
                payer = idx.by_id[owners[0]]
                credited = idx.by_id[t.member_id]
                flag(
                    "third_party_payment",
                    "low",
                    t,
                    f"{t.reference}: paid from {payer.name}'s phone into {credited.name}'s account. Normal for family payments; check if it repeats.",
                )

    # Unusually large single payment against a member's normal instalments.
    inst = defaultdict(int)
    for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id)):
        inst[ln.member_id] += ln.installment_cents
    for t in txns:
        if t.member_id and inst[t.member_id] and t.amount_cents >= 10 * inst[t.member_id] and t.amount_cents >= 10_000_000:
            flag(
                "large_payment",
                "medium",
                t,
                f"{t.reference}: KES {t.amount_cents / 100:,.0f} is over 10x this member's monthly instalments. Confirm source of funds (AML).",
            )
    return count
