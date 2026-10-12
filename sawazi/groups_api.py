"""MFI group lending: joint-liability groups, their meetings, and the cash they bank.

Members borrow individually; the whole group stands behind every member's loan. The group meets weekly or
fortnightly with its loan officer, who records on a meeting sheet what each member owed and paid. Payments by
M-Pesa are already matched to the member (they appear on the sheet for information). Cash paid at the meeting is
banked by the treasurer; it becomes money for the core system only when that bank deposit is matched to the sheet
and the amounts agree exactly. Sawazi never treats cash it has not seen banked as paid, and never guesses a split.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, auth
from .auth import Principal, require
from .db import get_session
from .models import (ExceptionItem, GroupMember, LendingGroup, Loan, MeetingSheet, Member, SheetLine,
                     StaffUser, Transaction)

router = APIRouter(tags=["groups"])

ROLES = ("member", "chair", "treasurer", "secretary")
FREQUENCIES = {"weekly": 7, "fortnightly": 14}
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _group(s: Session, institution_id: int, group_id: int, lock: bool = False) -> LendingGroup:
    q = select(LendingGroup).where(LendingGroup.id == group_id, LendingGroup.institution_id == institution_id)
    g = s.scalar(q.with_for_update() if lock else q)
    if g is None:
        raise HTTPException(404, "group not found")
    return g


def _active_members(s: Session, g: LendingGroup) -> list[tuple[GroupMember, Member]]:
    return list(s.execute(select(GroupMember, Member).join(Member, GroupMember.member_id == Member.id).where(
        GroupMember.institution_id == g.institution_id, GroupMember.group_id == g.id, GroupMember.left_on.is_(None))
        .order_by(Member.name)).all())


def summary(s: Session, g: LendingGroup) -> dict:
    """The group's portfolio. Joint liability: when one member is behind, every member stands behind that amount."""
    members = _active_members(s, g)
    ids = [m.id for _, m in members]
    loans = list(s.scalars(select(Loan).where(Loan.institution_id == g.institution_id, Loan.member_id.in_(ids),
                                              Loan.status == "active"))) if ids else []
    outstanding = sum(ln.balance_cents for ln in loans)
    arrears = sum(ln.arrears_cents for ln in loans)
    at_risk = sum(ln.balance_cents for ln in loans if ln.days_in_arrears > 30)
    behind = sorted({ln.member_id for ln in loans if ln.arrears_cents > 0})
    return {"members": len(members), "borrowers": len({ln.member_id for ln in loans}), "loans": len(loans),
            "outstanding_kes": outstanding / 100, "arrears_kes": arrears / 100,
            "par30_pct": round(100 * at_risk / outstanding, 1) if outstanding else 0.0,
            "members_behind": len(behind),
            # what each member stands behind for the others (joint liability), shown to staff only
            "shared_arrears_kes": arrears / 100}


def overview(s: Session, institution_id: int) -> dict | None:
    """Group lending across the institution, in cents and basis points (for snapshots and the board pack).
    None when the institution has no active groups."""
    groups = list(s.scalars(select(LendingGroup).where(LendingGroup.institution_id == institution_id,
                                                       LendingGroup.status == "active")))
    if not groups:
        return None
    rows = []
    for g in groups:
        x = summary(s, g)
        rows.append({"group_no": g.group_no, "name": g.name, "members": x["members"], "loans": x["loans"],
                     "outstanding_cents": round(x["outstanding_kes"] * 100), "arrears_cents": round(x["arrears_kes"] * 100),
                     "par30_bps": round(x["par30_pct"] * 100), "members_behind": x["members_behind"]})
    out = sum(r["outstanding_cents"] for r in rows)
    at_risk = sum(r["outstanding_cents"] * r["par30_bps"] // 10_000 for r in rows)
    behind = sorted((r for r in rows if r["arrears_cents"] > 0), key=lambda r: -r["arrears_cents"])
    return {"groups": len(rows), "members": sum(r["members"] for r in rows), "loans": sum(r["loans"] for r in rows),
            "outstanding_cents": out, "arrears_cents": sum(r["arrears_cents"] for r in rows),
            "par30_bps": round(10_000 * at_risk / out) if out else 0, "groups_behind": len(behind), "behind": behind[:10]}


def _group_out(s: Session, g: LendingGroup) -> dict:
    officer = s.get(StaffUser, g.officer_user_id) if g.officer_user_id else None
    return {"id": g.id, "group_no": g.group_no, "name": g.name, "status": g.status, "frequency": g.frequency,
            "meeting_day": DAYS[g.meeting_day] if g.meeting_day is not None else None,
            "officer": officer.name if officer else None, **summary(s, g)}


# ---------------------------------------------------------------- groups and their members

class GroupIn(BaseModel):
    group_no: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=200)
    meeting_day: str | None = Field(default=None, pattern="^(" + "|".join(DAYS) + ")$")
    frequency: str = Field(default="weekly", pattern="^(weekly|fortnightly)$")
    officer_email: str | None = Field(default=None, max_length=254)


class GroupMemberIn(BaseModel):
    member_no: str = Field(min_length=1, max_length=40)
    role: str = Field(default="member", pattern="^(" + "|".join(ROLES) + ")$")


@router.get("/institutions/{institution_id}/groups", dependencies=[Depends(require("read"))])
def list_groups(institution_id: int, s: Session = Depends(get_session)):
    groups = s.scalars(select(LendingGroup).where(LendingGroup.institution_id == institution_id)
                       .order_by(LendingGroup.status, LendingGroup.name))
    return [_group_out(s, g) for g in groups]


@router.post("/institutions/{institution_id}/groups")
def create_group(institution_id: int, body: GroupIn, s: Session = Depends(get_session),
                 who: Principal = Depends(require("groups"))):
    if s.scalar(select(LendingGroup.id).where(LendingGroup.institution_id == institution_id,
                                              LendingGroup.group_no == body.group_no.strip().upper())):
        raise HTTPException(409, f"group {body.group_no} already exists")
    officer = _officer(s, institution_id, body.officer_email)
    g = LendingGroup(institution_id=institution_id, group_no=body.group_no.strip().upper(), name=body.name.strip(),
                     meeting_day=DAYS.index(body.meeting_day) if body.meeting_day else None, frequency=body.frequency,
                     officer_user_id=officer.id if officer else None, status="active", created_at=auth.utcnow())
    s.add(g)
    s.flush()
    audit.record(s, who, "group.create", "group", g.id, after={"group_no": g.group_no, "name": g.name})
    s.commit()
    return _group_out(s, g)


def _officer(s: Session, institution_id: int, email: str | None) -> StaffUser | None:
    if not email:
        return None
    u = s.scalar(select(StaffUser).where(StaffUser.institution_id == institution_id,
                                         StaffUser.email == email.strip().lower(), StaffUser.is_active.is_(True)))
    if u is None:
        raise HTTPException(422, f"no active staff member with email {email}")
    return u


@router.get("/institutions/{institution_id}/groups/{group_id}", dependencies=[Depends(require("read"))])
def group_detail(institution_id: int, group_id: int, s: Session = Depends(get_session)):
    g = _group(s, institution_id, group_id)
    members = []
    for gm, m in _active_members(s, g):
        loans = s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.member_id == m.id,
                                             Loan.status == "active")).all()
        members.append({"member_no": m.member_no, "name": m.name, "role": gm.role, "joined_on": gm.joined_on,
                        "loans": [{"loan_no": ln.loan_no, "balance_kes": ln.balance_cents / 100,
                                   "installment_kes": ln.installment_cents / 100, "arrears_kes": ln.arrears_cents / 100,
                                   "days_in_arrears": ln.days_in_arrears} for ln in loans]})
    sheets = s.scalars(select(MeetingSheet).where(MeetingSheet.institution_id == institution_id,
                                                  MeetingSheet.group_id == g.id).order_by(MeetingSheet.meeting_on.desc())
                       .limit(12))
    return {**_group_out(s, g), "member_list": members,
            "sheets": [{"id": sh.id, "meeting_on": sh.meeting_on, "status": sh.status, "cash_kes": sh.cash_cents / 100}
                       for sh in sheets]}


@router.post("/institutions/{institution_id}/groups/{group_id}/members")
def add_member(institution_id: int, group_id: int, body: GroupMemberIn, s: Session = Depends(get_session),
               who: Principal = Depends(require("groups"))):
    g = _group(s, institution_id, group_id)
    m = s.scalar(select(Member).where(Member.institution_id == institution_id, Member.member_no == body.member_no.strip()))
    if m is None:
        raise HTTPException(404, "member not found")
    current = s.scalar(select(GroupMember).where(GroupMember.institution_id == institution_id,
                                                 GroupMember.member_id == m.id, GroupMember.left_on.is_(None)))
    if current and current.group_id != g.id:
        other = s.get(LendingGroup, current.group_id)
        raise HTTPException(409, f"{m.member_no} is already in group {other.group_no}; they must leave it first")
    if current:
        current.role = body.role
    else:
        s.add(GroupMember(institution_id=institution_id, group_id=g.id, member_id=m.id, role=body.role,
                          joined_on=auth.utcnow().date()))
    audit.record(s, who, "group.member_add", "group", g.id, after={"member_no": m.member_no, "role": body.role})
    s.commit()
    return group_detail(institution_id, group_id, s)


@router.post("/institutions/{institution_id}/groups/{group_id}/members/{member_no}/leave")
def member_leaves(institution_id: int, group_id: int, member_no: str, note: str, s: Session = Depends(get_session),
                  who: Principal = Depends(require("groups"))):
    """A member leaves the group. Their loans stay theirs; the group's liability for them ends only as the
    institution's policy says, which is recorded in the note."""
    g = _group(s, institution_id, group_id)
    gm = s.scalar(select(GroupMember).join(Member, GroupMember.member_id == Member.id).where(
        GroupMember.group_id == g.id, GroupMember.left_on.is_(None), Member.member_no == member_no))
    if gm is None:
        raise HTTPException(404, "not an active member of this group")
    if len(note.strip()) < 3:
        raise HTTPException(422, "give the reason")
    gm.left_on = auth.utcnow().date()
    audit.record(s, who, "group.member_leave", "group", g.id, after={"member_no": member_no}, note=note)
    s.commit()
    return {"left": True}


# ---------------------------------------------------------------- meeting sheets

class SheetIn(BaseModel):
    meeting_on: date


class LineIn(BaseModel):
    member_no: str = Field(max_length=40)
    cash_kes: Decimal = Field(default=Decimal(0), ge=0, max_digits=12, decimal_places=2)
    present: bool = True
    note: str | None = Field(default=None, max_length=200)


class LinesIn(BaseModel):
    lines: list[LineIn] = Field(max_length=200)


def _sheet(s: Session, institution_id: int, group_id: int, sheet_id: int, lock: bool = False) -> MeetingSheet:
    q = select(MeetingSheet).where(MeetingSheet.id == sheet_id, MeetingSheet.group_id == group_id,
                                   MeetingSheet.institution_id == institution_id)
    sh = s.scalar(q.with_for_update() if lock else q)
    if sh is None:
        raise HTTPException(404, "meeting sheet not found")
    return sh


def _sheet_out(s: Session, sh: MeetingSheet) -> dict:
    lines = []
    for ln, m in s.execute(select(SheetLine, Member).join(Member, SheetLine.member_id == Member.id)
                           .where(SheetLine.sheet_id == sh.id).order_by(Member.name)):
        loan = s.get(Loan, ln.loan_id) if ln.loan_id else None
        lines.append({"member_no": m.member_no, "name": m.name, "loan_no": loan.loan_no if loan else None,
                      "due_kes": ln.due_cents / 100, "mpesa_kes": ln.mpesa_cents / 100, "cash_kes": ln.cash_cents / 100,
                      "short_kes": max(ln.due_cents - ln.mpesa_cents - ln.cash_cents, 0) / 100,
                      "present": ln.present, "note": ln.note})
    g = s.get(LendingGroup, sh.group_id)
    return {"id": sh.id, "group_id": g.id, "group_no": g.group_no, "group": g.name, "meeting_on": sh.meeting_on,
            "status": sh.status, "lines": lines,
            "totals": {k: round(sum(x[f"{k}_kes"] for x in lines), 2) for k in ("due", "mpesa", "cash", "short")},
            "banked_reference": s.get(Transaction, sh.bank_transaction_id).reference if sh.bank_transaction_id else None}


@router.post("/institutions/{institution_id}/groups/{group_id}/sheets")
def new_sheet(institution_id: int, group_id: int, body: SheetIn, s: Session = Depends(get_session),
              who: Principal = Depends(require("groups"))):
    """A sheet for one meeting: each member's instalment plus anything overdue, and what they already paid by M-Pesa
    since the last meeting. The officer then records the cash."""
    g = _group(s, institution_id, group_id)
    if body.meeting_on > auth.utcnow().date() + timedelta(days=1):
        raise HTTPException(422, "a meeting sheet cannot be for a future date")
    if s.scalar(select(MeetingSheet.id).where(MeetingSheet.group_id == g.id, MeetingSheet.meeting_on == body.meeting_on)):
        raise HTTPException(409, "there is already a sheet for that meeting")
    since = body.meeting_on - timedelta(days=FREQUENCIES.get(g.frequency, 7))
    sh = MeetingSheet(institution_id=institution_id, group_id=g.id, meeting_on=body.meeting_on, status="draft",
                      created_by_user_id=who.id, created_at=auth.utcnow())
    s.add(sh)
    s.flush()
    for _, m in _active_members(s, g):
        loan = s.scalar(select(Loan).where(Loan.institution_id == institution_id, Loan.member_id == m.id,
                                           Loan.status == "active").order_by(Loan.days_in_arrears.desc(), Loan.id))
        due = min(loan.installment_cents + loan.arrears_cents, loan.balance_cents) if loan else 0
        paid = s.scalar(select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
            Transaction.institution_id == institution_id, Transaction.member_id == m.id,
            Transaction.status == "allocated", Transaction.source.in_(("mpesa", "bank")),
            Transaction.txn_time > since, Transaction.txn_time < body.meeting_on + timedelta(days=1)))
        s.add(SheetLine(institution_id=institution_id, sheet_id=sh.id, member_id=m.id, loan_id=loan.id if loan else None,
                        due_cents=due, mpesa_cents=int(paid), cash_cents=0, present=True))
    audit.record(s, who, "group.sheet_create", "meeting_sheet", sh.id, after={"group_no": g.group_no,
                                                                          "meeting_on": str(body.meeting_on)})
    s.commit()
    return _sheet_out(s, sh)


@router.get("/institutions/{institution_id}/groups/{group_id}/sheets/{sheet_id}",
            dependencies=[Depends(require("read"))])
def get_sheet(institution_id: int, group_id: int, sheet_id: int, s: Session = Depends(get_session)):
    return _sheet_out(s, _sheet(s, institution_id, group_id, sheet_id))


@router.put("/institutions/{institution_id}/groups/{group_id}/sheets/{sheet_id}")
def record_meeting(institution_id: int, group_id: int, sheet_id: int, body: LinesIn, s: Session = Depends(get_session),
                   who: Principal = Depends(require("groups"))):
    sh = _sheet(s, institution_id, group_id, sheet_id, lock=True)
    if sh.status != "draft":
        raise HTTPException(409, f"this sheet is {sh.status}; it can no longer be changed")
    lines = {m.member_no: ln for ln, m in s.execute(select(SheetLine, Member).join(Member, SheetLine.member_id == Member.id)
                                                     .where(SheetLine.sheet_id == sh.id))}
    for x in body.lines:
        ln = lines.get(x.member_no)
        if ln is None:
            raise HTTPException(422, f"{x.member_no} is not on this sheet")
        ln.cash_cents, ln.present, ln.note = int(x.cash_kes * 100), x.present, x.note
    audit.record(s, who, "group.sheet_record", "meeting_sheet", sh.id,
                 after={"cash_cents": sum(ln.cash_cents for ln in lines.values())})
    s.commit()
    return _sheet_out(s, sh)


@router.post("/institutions/{institution_id}/groups/{group_id}/sheets/{sheet_id}/submit")
def submit_sheet(institution_id: int, group_id: int, sheet_id: int, s: Session = Depends(get_session),
                 who: Principal = Depends(require("groups"))):
    """Close the meeting: the cash total is fixed and now waits for the group's bank deposit."""
    sh = _sheet(s, institution_id, group_id, sheet_id, lock=True)
    if sh.status != "draft":
        raise HTTPException(409, f"this sheet is already {sh.status}")
    sh.cash_cents = s.scalar(select(func.coalesce(func.sum(SheetLine.cash_cents), 0)).where(SheetLine.sheet_id == sh.id))
    sh.status = "banked" if sh.cash_cents == 0 else "submitted"  # no cash collected: nothing to bank
    sh.submitted_at = auth.utcnow()
    if sh.status == "banked":
        sh.banked_at = sh.submitted_at
    audit.record(s, who, "group.sheet_submit", "meeting_sheet", sh.id, after={"cash_cents": sh.cash_cents,
                                                                          "status": sh.status})
    s.commit()
    return _sheet_out(s, sh)


@router.get("/institutions/{institution_id}/groups/{group_id}/sheets/{sheet_id}/deposits",
            dependencies=[Depends(require("read"))])
def candidate_deposits(institution_id: int, group_id: int, sheet_id: int, s: Session = Depends(get_session)):
    """Bank or M-Pesa payments not yet matched that could be this meeting's banked cash: the same amount, within a
    week after the meeting. A person chooses; nothing is picked automatically."""
    sh = _sheet(s, institution_id, group_id, sheet_id)
    if sh.status != "submitted":
        return []
    rows = s.scalars(select(Transaction).where(
        Transaction.institution_id == institution_id, Transaction.status.in_(("unmatched", "suspense")),
        Transaction.amount_cents == sh.cash_cents, Transaction.source.in_(("bank", "mpesa")),
        Transaction.txn_time >= sh.meeting_on, Transaction.txn_time < sh.meeting_on + timedelta(days=8))
        .order_by(Transaction.txn_time))
    return [{"transaction_id": t.id, "source": t.source, "reference": t.reference, "date": t.txn_time,
             "kes": t.amount_cents / 100, "narrative": t.narrative or t.payer_name or ""} for t in rows]


class BankIn(BaseModel):
    transaction_id: int
    note: str | None = Field(default=None, max_length=300)


@router.post("/institutions/{institution_id}/groups/{group_id}/sheets/{sheet_id}/bank")
def match_banked_cash(institution_id: int, group_id: int, sheet_id: int, body: BankIn,
                      s: Session = Depends(get_session), who: Principal = Depends(require("resolve"))):
    """The group's deposit for this meeting's cash has arrived. Only if the amounts agree exactly does each member's
    cash become a payment to their loan (and so reach the postings file). The deposit itself is never allocated a
    second time."""
    from .api import matching_lock
    from .engine.matching import allocate

    with matching_lock(s, institution_id):
        s.expire_all()
        sh = _sheet(s, institution_id, group_id, sheet_id, lock=True)
        if sh.status != "submitted":
            raise HTTPException(409, f"this sheet is {sh.status}, not waiting for its bank deposit")
        t = s.scalar(select(Transaction).where(Transaction.id == body.transaction_id,
                                               Transaction.institution_id == institution_id).with_for_update())
        if t is None or t.status not in ("unmatched", "suspense"):
            raise HTTPException(409, "that payment is not available to match")
        if t.amount_cents != sh.cash_cents:
            raise HTTPException(409, f"the deposit is KES {t.amount_cents / 100:,.2f} but the meeting collected "
                                     f"KES {sh.cash_cents / 100:,.2f}. Nothing was changed: find the difference first.")
        g = s.get(LendingGroup, sh.group_id)
        allocated = []
        for ln, m in s.execute(select(SheetLine, Member).join(Member, SheetLine.member_id == Member.id)
                               .where(SheetLine.sheet_id == sh.id, SheetLine.cash_cents > 0)):
            cash = Transaction(institution_id=institution_id, source="group_cash",
                               reference=f"{g.group_no}-{sh.meeting_on:%Y%m%d}-{m.member_no}"[:80],
                               txn_time=t.txn_time, amount_cents=ln.cash_cents, payer_name=m.name,
                               account_ref=m.member_no, narrative=f"Cash at {g.name} meeting {sh.meeting_on}, banked {t.reference}",
                               status="unmatched", member_id=m.id, match_method="group_sheet", match_confidence=100)
            s.add(cash)
            s.flush()
            allocated += allocate(s, cash, m.id, ln.loan_id)
        t.status, t.match_method, t.match_confidence = "banked_sheet", "group_sheet", 100
        for e in s.scalars(select(ExceptionItem).where(ExceptionItem.institution_id == institution_id,
                                                       ExceptionItem.transaction_id == t.id,
                                                       ExceptionItem.status == "open")):
            e.status, e.detail = "resolved", e.detail + f" | Banked cash of {g.name} meeting {sh.meeting_on}"
        sh.status, sh.banked_at, sh.bank_transaction_id = "banked", auth.utcnow(), t.id
        from .guarantors import release_repaid
        release_repaid(s, institution_id)
        audit.record(s, who, "group.sheet_banked", "meeting_sheet", sh.id, note=body.note,
                     after={"bank_reference": t.reference, "cash_cents": sh.cash_cents,
                            "allocations": [{"target": a.target, "loan_id": a.loan_id, "amount_cents": a.amount_cents}
                                            for a in allocated]})
        s.commit()
    return _sheet_out(s, sh)


# ---------------------------------------------------------------- a payment for several members

class SplitLine(BaseModel):
    member_no: str = Field(max_length=40)
    amount_kes: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class SplitIn(BaseModel):
    lines: list[SplitLine] = Field(min_length=2, max_length=60)
    note: str = Field(min_length=3, max_length=300)


@router.post("/exceptions/{exception_id}/split")
def split_payment(exception_id: int, body: SplitIn, s: Session = Depends(get_session),
                  who: Principal = Depends(require("resolve"))):
    """One payment in suspense that is really several members' money (a treasurer paying for the group). A person
    gives each member's share; the shares must add up to the payment exactly. Each share becomes its own payment."""
    from .api import matching_lock
    from .engine.matching import allocate

    with matching_lock(s, who.institution_id):
        s.expire_all()
        e = s.get(ExceptionItem, exception_id)
        if not e or e.status != "open" or e.kind != "suspense" or e.institution_id != who.institution_id:
            raise HTTPException(404, "open suspense item not found")
        t = s.scalar(select(Transaction).where(Transaction.id == e.transaction_id).with_for_update())
        if t is None or t.status != "suspense":
            raise HTTPException(409, "that payment is not in suspense")
        shares = [int(x.amount_kes * 100) for x in body.lines]
        if sum(shares) != t.amount_cents:
            raise HTTPException(422, f"the shares add up to KES {sum(shares) / 100:,.2f}, but the payment is "
                                     f"KES {t.amount_cents / 100:,.2f}. They must agree exactly.")
        members = {}
        for x in body.lines:
            m = s.scalar(select(Member).where(Member.institution_id == t.institution_id, Member.member_no == x.member_no))
            if m is None:
                raise HTTPException(404, f"member {x.member_no} not found")
            members[x.member_no] = m
        if len(members) != len(body.lines):
            raise HTTPException(422, "each member appears once")
        out = []
        for n, (x, cents) in enumerate(zip(body.lines, shares), start=1):
            m = members[x.member_no]
            part = Transaction(institution_id=t.institution_id, source=t.source, reference=f"{t.reference}/{n}"[:80],
                               txn_time=t.txn_time, amount_cents=cents, payer_name=t.payer_name,
                               payer_phone=t.payer_phone, account_ref=m.member_no,
                               narrative=f"Share of {t.reference}: {body.note}"[:500], status="unmatched",
                               match_method="manual_split", match_confidence=100)
            s.add(part)
            s.flush()
            allocs = allocate(s, part, m.id)
            out.append({"member_no": m.member_no, "amount_kes": cents / 100,
                        "allocations": [{"target": a.target, "amount_kes": a.amount_cents / 100} for a in allocs]})
        t.status, t.match_method = "split", "manual_split"
        e.status, e.detail = "resolved", e.detail + f" | Split across {len(out)} members: {body.note}"
        from .guarantors import release_repaid
        release_repaid(s, t.institution_id)
        audit.record(s, who, "suspense.split", "exception", e.id, note=body.note,
                     before={"reference": t.reference, "amount_cents": t.amount_cents},
                     after={"shares": [{"member_no": x.member_no, "amount_cents": c} for x, c in zip(body.lines, shares)]})
        s.commit()
    return {"resolved": True, "shares": out}
