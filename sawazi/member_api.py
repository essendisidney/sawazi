"""Member app API (/m/...): members sign in on their own phone and see only their own records.

Sign-in: phone number -> one-time SMS code -> choose the membership (one phone can belong to several members, even
at several SACCOs) -> set an app PIN on this phone. After that: this phone + PIN. A new phone needs a new SMS code,
which is also how a forgotten PIN is replaced.

Separation: member tokens start "mbr_", live in their own table and are checked only here. A member token can never
reach a staff endpoint, and a staff token can never reach these. Every query is filtered to the signed-in member.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import audit, auth, sms
from .auth import Principal, require, utcnow
from .db import get_session
from .importers.common import norm_phone
from .models import (Institution, Member, MemberCredential, MemberDevice, MemberOtp, MemberSession, SmsMessage,
                     SmsSettings)

router = APIRouter(tags=["member app"])

TOKEN_PREFIX = "mbr_"
OTP_MINUTES, OTP_ATTEMPTS, OTP_PER_HOUR = 10, 5, 3
VERIFY_MINUTES = 10
SESSION_MINUTES = 30
PIN_ATTEMPTS, PIN_LOCK_MINUTES = 5, 15


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def weak_pin(pin: str) -> str | None:
    """A reason the PIN is too easy to guess, or None."""
    if not pin.isdigit() or not 4 <= len(pin) <= 6:
        return "Use 4 to 6 digits."
    if len(set(pin)) == 1:
        return "Don't use the same digit repeated."
    digits = [int(c) for c in pin]
    steps = {b - a for a, b in zip(digits, digits[1:])}
    if steps in ({1}, {-1}):
        return "Don't use digits in sequence."
    return None


# ---------------------------------------------------------------- who is signed in

@dataclass
class SignedIn:
    member: Member
    session: MemberSession

    @property
    def principal(self) -> Principal:
        return Principal("member", self.member.id, self.member.institution_id, "", self.member.name)


def current_member(authorization: str | None = Header(default=None), s: Session = Depends(get_session)) -> SignedIn:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.startswith(TOKEN_PREFIX):
        raise HTTPException(401, "please sign in", headers={"WWW-Authenticate": "Bearer"})
    sess = s.scalar(select(MemberSession).where(MemberSession.token_hash == _hash(token)))
    if not sess or sess.revoked_at or sess.expires_at <= utcnow():
        raise HTTPException(401, "your session has ended, please sign in again", headers={"WWW-Authenticate": "Bearer"})
    device = s.get(MemberDevice, sess.device_id)
    member = s.get(Member, sess.member_id)
    if device is None or device.revoked_at or member is None or member.institution_id != sess.institution_id:
        raise HTTPException(401, "please sign in again", headers={"WWW-Authenticate": "Bearer"})
    return SignedIn(member, sess)


def _new_session(s: Session, member: Member, device: MemberDevice) -> str:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    s.add(MemberSession(institution_id=member.institution_id, member_id=member.id, device_id=device.id,
                        token_hash=_hash(token), created_at=now, expires_at=now + timedelta(minutes=SESSION_MINUTES)))
    device.last_used_at = now
    return token


def _member_out(s: Session, m: Member) -> dict:
    inst = s.get(Institution, m.institution_id)
    return {"member_no": m.member_no, "name": m.name, "institution": inst.name if inst else ""}


# ---------------------------------------------------------------- sign in on a new phone

class OtpIn(BaseModel):
    phone: str = Field(max_length=30)


class VerifyIn(BaseModel):
    phone: str = Field(max_length=30)
    code: str = Field(max_length=10)


class DeviceIn(BaseModel):
    verify_token: str = Field(max_length=100)
    membership: int  # which of the memberships listed after verification
    pin: str = Field(max_length=10)
    label: str | None = Field(default=None, max_length=100)


class LoginIn(BaseModel):
    device_token: str = Field(max_length=100)
    pin: str = Field(max_length=10)


def _memberships(s: Session, phone: str) -> list[Member]:
    return list(s.scalars(select(Member).where(Member.phone == phone).order_by(Member.institution_id, Member.member_no)))


@router.post("/m/otp")
def send_otp(body: OtpIn, s: Session = Depends(get_session), provider=Depends(sms.get_provider)):
    """Text a one-time code. Always the same answer, whether or not the number is registered, so the app never
    reveals who is a member. Unregistered numbers get no SMS (no cost, no spam)."""
    phone = norm_phone(body.phone)
    if not phone:
        raise HTTPException(422, "Enter a Kenyan mobile number, e.g. 0712 345 678.")
    members = _memberships(s, phone)
    recent = s.scalar(select(func.count()).select_from(MemberOtp).where(
        MemberOtp.phone == phone, MemberOtp.created_at > utcnow() - timedelta(hours=1)))
    if members and recent < OTP_PER_HOUR:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        now = utcnow()
        s.add(MemberOtp(phone=phone, code_hash=_hash(f"{phone}:{code}"), created_at=now,
                        expires_at=now + timedelta(minutes=OTP_MINUTES)))
        s.commit()
        first = members[0]
        settings = s.scalar(select(SmsSettings).where(SmsSettings.institution_id == first.institution_id))
        text = (f"Sawazi: your sign-in code is {code}. It works for {OTP_MINUTES} minutes. "
                "Never share it, not even with SACCO staff.")
        try:
            res = provider.send(phone, text, settings.service_name if settings else None)
            s.add(SmsMessage(institution_id=first.institution_id, member_id=first.id, phone=phone,
                             body=text.replace(code, "######"), provider=provider.name, status=res.status,
                             provider_message_id=res.provider_message_id, approved_by_user_id=None,
                             approved_at=now, sent_at=now if res.status != "failed" else None))
            s.commit()
        except Exception:  # the member can ask again; nothing else depends on this SMS
            s.rollback()
    return {"sent": True, "message": "If this number is registered with a SACCO on Sawazi, a code is on its way."}


@router.post("/m/otp/verify")
def verify_otp(body: VerifyIn, s: Session = Depends(get_session)):
    phone = norm_phone(body.phone)
    otp = s.scalar(select(MemberOtp).where(MemberOtp.phone == phone, MemberOtp.verify_token_hash.is_(None))
                   .order_by(MemberOtp.id.desc()).limit(1).with_for_update()) if phone else None
    if otp is None or otp.expires_at <= utcnow() or otp.attempts >= OTP_ATTEMPTS:
        raise HTTPException(400, "That code has expired. Ask for a new one.")
    if not hmac.compare_digest(_hash(f"{phone}:{body.code.strip()}"), otp.code_hash):
        otp.attempts += 1
        s.commit()
        left = OTP_ATTEMPTS - otp.attempts
        raise HTTPException(400, f"That code is not right. {left} tries left." if left else "Too many tries. Ask for a new code.")
    token = secrets.token_urlsafe(24)
    otp.verify_token_hash = _hash(token)
    s.commit()
    members = _memberships(s, phone)
    return {"verify_token": token,
            "memberships": [{"id": m.id, "institution": s.get(Institution, m.institution_id).name,
                             "member_no": m.member_no, "first_name": m.name.split()[0].title()} for m in members]}


@router.post("/m/devices")
def register_device(body: DeviceIn, request: Request, s: Session = Depends(get_session)):
    """Set the app PIN on this phone. Replaces any earlier PIN for this membership (that is how a forgotten PIN
    is reset), and signs in."""
    otp = s.scalar(select(MemberOtp).where(MemberOtp.verify_token_hash == _hash(body.verify_token)).with_for_update())
    if otp is None or otp.used_at or otp.created_at <= utcnow() - timedelta(minutes=OTP_MINUTES + VERIFY_MINUTES):
        raise HTTPException(400, "Please start again: ask for a new code.")
    member = s.get(Member, body.membership)
    if member is None or member.phone != otp.phone:  # only a membership of the phone that received the code
        raise HTTPException(400, "Please start again: ask for a new code.")
    reason = weak_pin(body.pin)
    if reason:
        raise HTTPException(422, reason)
    now = utcnow()
    otp.used_at = now
    cred = s.scalar(select(MemberCredential).where(MemberCredential.member_id == member.id))
    if cred is None:
        cred = MemberCredential(institution_id=member.institution_id, member_id=member.id)
        s.add(cred)
    cred.pin_hash, cred.failed_attempts, cred.locked_until, cred.updated_at = auth.hash_password(body.pin), 0, None, now
    device_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    device = MemberDevice(institution_id=member.institution_id, member_id=member.id, token_hash=_hash(device_token),
                          label=(body.label or request.headers.get("user-agent", ""))[:100], created_at=now)
    s.add(device)
    s.flush()
    who = Principal("member", member.id, member.institution_id, "", member.name)
    audit.record(s, who, "member_app.device_added", "member_device", device.id, after={"label": device.label},
                 ip=request.client.host if request.client else None)
    session = _new_session(s, member, device)
    s.commit()
    return {"device_token": device_token, "session_token": session, "member": _member_out(s, member),
            "session_minutes": SESSION_MINUTES}


@router.post("/m/login")
def login(body: LoginIn, s: Session = Depends(get_session)):
    """This phone + PIN. Five wrong PINs lock the app for 15 minutes."""
    device = s.scalar(select(MemberDevice).where(MemberDevice.token_hash == _hash(body.device_token)))
    if device is None or device.revoked_at:
        raise HTTPException(401, "This phone is not set up. Sign in with an SMS code.")
    cred = s.scalar(select(MemberCredential).where(MemberCredential.member_id == device.member_id).with_for_update())
    now = utcnow()
    if cred is None:
        raise HTTPException(401, "This phone is not set up. Sign in with an SMS code.")
    if cred.locked_until and cred.locked_until > now:
        mins = max(1, int((cred.locked_until - now).total_seconds() // 60) + 1)
        raise HTTPException(423, f"Too many wrong PINs. Try again in {mins} minutes, or sign in with an SMS code.")
    if not auth.verify_password(body.pin, cred.pin_hash):
        cred.failed_attempts += 1
        left = PIN_ATTEMPTS - cred.failed_attempts
        if left <= 0:
            cred.failed_attempts, cred.locked_until = 0, now + timedelta(minutes=PIN_LOCK_MINUTES)
        s.commit()
        raise HTTPException(401, f"Wrong PIN. {left} tries left." if left > 0 else
                            f"Too many wrong PINs. Try again in {PIN_LOCK_MINUTES} minutes.")
    cred.failed_attempts, cred.locked_until = 0, None
    member = s.get(Member, device.member_id)
    token = _new_session(s, member, device)
    s.commit()
    return {"session_token": token, "member": _member_out(s, member), "session_minutes": SESSION_MINUTES}


@router.post("/m/logout")
def logout(me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    me.session.revoked_at = utcnow()
    s.commit()
    return {"signed_out": True}


@router.get("/m/me")
def whoami(me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    return _member_out(s, me.member)


# ---------------------------------------------------------------- staff: switch off a member's app access

@router.post("/institutions/{institution_id}/members/{member_no}/app-access/revoke", tags=["members"])
def revoke_app_access(institution_id: int, member_no: str, note: str, s: Session = Depends(get_session),
                      who: Principal = Depends(require("manage_users"))):
    """Sign a member out of the app on every phone (lost phone, suspected fraud). They can sign in again only
    with an SMS code to their registered number."""
    m = s.scalar(select(Member).where(Member.institution_id == institution_id, Member.member_no == member_no))
    if not m:
        raise HTTPException(404, "member not found")
    now = utcnow()
    n = s.execute(update(MemberDevice).where(MemberDevice.member_id == m.id, MemberDevice.revoked_at.is_(None))
                  .values(revoked_at=now)).rowcount
    s.execute(update(MemberSession).where(MemberSession.member_id == m.id, MemberSession.revoked_at.is_(None))
              .values(revoked_at=now))
    audit.record(s, who, "member_app.revoke", "member", m.id, after={"devices_revoked": n}, note=note)
    s.commit()
    return {"member_no": member_no, "devices_revoked": n}


# ---------------------------------------------------------------- the member's own money

def _kes(cents: int | None) -> float | None:
    return None if cents is None else cents / 100


@router.get("/m/overview")
def overview(me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    """Balances, loans, how to pay each one, and the latest payments Sawazi has matched to this member."""
    from .models import Allocation, Loan, Transaction

    m = me.member
    inst = s.get(Institution, m.institution_id)
    loans = list(s.scalars(select(Loan).where(Loan.institution_id == m.institution_id, Loan.member_id == m.id,
                                              Loan.status == "active").order_by(Loan.days_in_arrears.desc())))
    pays = list(s.scalars(select(Transaction).where(Transaction.institution_id == m.institution_id,
                                                    Transaction.member_id == m.id, Transaction.status == "allocated")
                          .order_by(Transaction.txn_time.desc()).limit(10)))
    split: dict[int, list] = {}
    if pays:
        for a in s.scalars(select(Allocation).where(Allocation.institution_id == m.institution_id,
                                                    Allocation.transaction_id.in_([t.id for t in pays]))):
            split.setdefault(a.transaction_id, []).append({"to": a.target, "kes": a.amount_cents / 100})
    paybill = inst.paybill if inst else None
    return {
        "member": _member_out(s, m),
        "deposits_kes": _kes(m.deposits_cents), "shares_kes": _kes(m.shares_cents),
        "balances_as_of": m.balances_as_of,
        "loans": [{"loan_no": ln.loan_no, "product": ln.product, "balance_kes": ln.balance_cents / 100,
                   "arrears_kes": ln.arrears_cents / 100, "days_in_arrears": ln.days_in_arrears,
                   "installment_kes": ln.installment_cents / 100, "next_due_on": ln.next_due_on,
                   "pay": {"paybill": paybill, "account": ln.loan_no}} for ln in loans],
        "pay_deposits": {"paybill": paybill, "account": m.member_no},
        "payments": [{"date": t.txn_time, "source": t.source, "reference": t.reference, "kes": t.amount_cents / 100,
                      "split": split.get(t.id, [])} for t in pays],
    }


# ---------------------------------------------------------------- guarantees

class AnswerIn(BaseModel):
    answer: str = Field(pattern="^(accept|decline)$")


def _short_name(mem: Member | None) -> str:
    if mem is None:
        return "?"
    parts = mem.name.title().split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0]


@router.get("/m/guarantees")
def my_guarantees(me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    """Requests waiting for this member, what they guarantee for others, and who guarantees their loans."""
    from . import guarantors
    from .models import CoreGuarantee, Guarantee, Loan, LoanApplication

    m = me.member
    waiting, giving = [], []
    for g, a in s.execute(select(Guarantee, LoanApplication)
                          .join(LoanApplication, Guarantee.application_id == LoanApplication.id)
                          .where(Guarantee.institution_id == m.institution_id, Guarantee.guarantor_member_id == m.id)
                          .order_by(Guarantee.id.desc())):
        st = guarantors.effective_status(g)
        row = {"id": g.id, "for": _short_name(s.get(Member, a.member_id)), "kes": g.amount_cents / 100,
               "loan_kes": a.amount_cents / 100, "status": st}
        if st == "requested" and a.status in guarantors.OPEN_APPLICATION:
            waiting.append(row)
        elif st == "accepted":
            giving.append(row | {"source": "sawazi"})
    for cg, ln in s.execute(select(CoreGuarantee, Loan).join(Loan, CoreGuarantee.loan_id == Loan.id).where(
            CoreGuarantee.institution_id == m.institution_id, CoreGuarantee.guarantor_member_id == m.id,
            CoreGuarantee.status == "active", guarantors.core_counts())):
        giving.append({"id": None, "for": _short_name(s.get(Member, ln.member_id)), "kes": cg.amount_cents / 100,
                       "loan_no": ln.loan_no, "status": "accepted", "source": "core"})
    mine = []
    my_loans = {ln.id: ln for ln in s.scalars(select(Loan).where(Loan.institution_id == m.institution_id,
                                                                 Loan.member_id == m.id, Loan.status == "active"))}
    if my_loans:
        for cg in s.scalars(select(CoreGuarantee).where(CoreGuarantee.institution_id == m.institution_id,
                                                        CoreGuarantee.loan_id.in_(list(my_loans)),
                                                        CoreGuarantee.status == "active", guarantors.core_counts())):
            mine.append({"by": _short_name(s.get(Member, cg.guarantor_member_id)), "kes": cg.amount_cents / 100,
                         "loan_no": my_loans[cg.loan_id].loan_no, "status": "accepted"})
    for g, a in s.execute(select(Guarantee, LoanApplication)
                          .join(LoanApplication, Guarantee.application_id == LoanApplication.id)
                          .where(LoanApplication.institution_id == m.institution_id, LoanApplication.member_id == m.id,
                                 Guarantee.status.in_(("accepted", "requested")))):
        loan = s.get(Loan, a.disbursed_loan_id) if a.disbursed_loan_id else None
        mine.append({"by": _short_name(s.get(Member, g.guarantor_member_id)), "kes": g.amount_cents / 100,
                     "loan_no": loan.loan_no if loan else f"application SWZ-{a.id}",
                     "status": guarantors.effective_status(g)})
    free = guarantors.free_capacity(s, m)
    return {"waiting": waiting, "giving": giving, "mine": mine,
            "pledged_kes": guarantors.pledged_cents(s, m.institution_id, m.id) / 100,
            "can_still_guarantee_kes": None if free is None else max(free, 0) / 100}


@router.post("/m/guarantees/{guarantee_id}/answer")
def answer_guarantee(guarantee_id: int, body: AnswerIn, request: Request, me: SignedIn = Depends(current_member),
                     s: Session = Depends(get_session)):
    """Accept or decline a request addressed to this member. Signed in with this phone and PIN, so no extra code is
    needed; capacity is checked under the same lock as the SMS link and USSD."""
    from . import guarantors
    from .models import Guarantee

    g = s.get(Guarantee, guarantee_id)
    if g is None or g.guarantor_member_id != me.member.id or g.institution_id != me.member.institution_id:
        raise HTTPException(404, "request not found")
    result = guarantors.record_answer(s, g.id, body.answer, request.client.host if request.client else None, "app")
    if result in ("closed", "capacity"):
        s.rollback()
        raise HTTPException(409, "This request is no longer open." if result == "closed"
                            else "Your deposits no longer cover this amount. Please talk to your SACCO.")
    s.commit()
    return {"status": result}


# ---------------------------------------------------------------- applying for a loan

class CheckIn(BaseModel):
    product_id: int
    amount_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    term_months: int = Field(ge=1, le=240)


class ApplyIn(CheckIn):
    purpose: str | None = Field(default=None, max_length=300)
    guarantors: list[str] = Field(default_factory=list, max_length=6)


MAX_OPEN_APPLICATIONS = 2


def _product(s: Session, me: SignedIn, product_id: int):
    from .models import LoanProduct

    p = s.get(LoanProduct, product_id)
    if p is None or p.institution_id != me.member.institution_id or not p.active:
        raise HTTPException(404, "product not found")
    return p


def _indicative(s: Session, me: SignedIn, p, amount_cents: int, term: int):
    from .engine import appraisal
    from .models import Loan

    m = me.member
    rules = appraisal.Product(p.min_amount_cents, p.max_amount_cents, p.max_term_months, p.interest_rate_bps,
                              p.interest_method, p.deposits_multiplier_pct, p.min_membership_months, p.max_arrears_days,
                              p.one_third_rule, p.guarantor_cover, p.min_guarantors)
    loans = [appraisal.ExistingLoan(ln.loan_no, ln.balance_cents, ln.days_in_arrears) for ln in s.scalars(
        select(Loan).where(Loan.institution_id == m.institution_id, Loan.member_id == m.id, Loan.status == "active"))]
    facts = appraisal.MemberFacts(m.joined_on, m.deposits_cents, m.gross_pay_cents, m.net_pay_cents)
    return appraisal.appraise(amount_cents, term, rules, facts, loans, [], utcnow().date())


@router.get("/m/products")
def products(me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    from .models import LoanProduct

    return [{"id": p.id, "name": p.name, "min_kes": p.min_amount_cents / 100, "max_kes": p.max_amount_cents / 100,
             "max_months": p.max_term_months, "rate_pct": p.interest_rate_bps / 100}
            for p in s.scalars(select(LoanProduct).where(LoanProduct.institution_id == me.member.institution_id,
                                                         LoanProduct.active.is_(True)).order_by(LoanProduct.name))]


@router.post("/m/loan-check")
def loan_check(body: CheckIn, me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    """An indicative check before applying. Not a decision: the SACCO decides."""
    p = _product(s, me, body.product_id)
    a = _indicative(s, me, p, int(body.amount_kes * 100), body.term_months)
    return {"outcome": a.outcome, "instalment_kes": a.instalment_cents / 100,
            "max_eligible_kes": a.max_eligible_cents / 100, "max_eligible_partial": a.max_eligible_partial,
            "guarantor_cover_needed_kes": a.required_cover_cents / 100,
            "checks": [{"code": c.code, "status": c.status, "message": c.message} for c in a.checks
                       if c.code != "guarantors"]}


@router.get("/m/applications")
def my_applications(me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    from .models import LoanApplication, LoanProduct

    rows = s.scalars(select(LoanApplication).where(LoanApplication.institution_id == me.member.institution_id,
                                                   LoanApplication.member_id == me.member.id)
                     .order_by(LoanApplication.id.desc()).limit(20))
    return [{"ref": f"SWZ-{a.id}", "product": s.get(LoanProduct, a.product_id).name, "kes": a.amount_cents / 100,
             "months": a.term_months, "status": a.status, "created_at": a.created_at} for a in rows]


@router.post("/m/applications")
def apply(body: ApplyIn, me: SignedIn = Depends(current_member), s: Session = Depends(get_session)):
    """Send an application to the SACCO. It arrives as a draft for a credit officer, who checks it, asks the
    guarantors and submits it; approval works exactly as for any other loan."""
    from .models import LoanApplication

    m = me.member
    p = _product(s, me, body.product_id)
    open_count = s.scalar(select(func.count()).select_from(LoanApplication).where(
        LoanApplication.institution_id == m.institution_id, LoanApplication.member_id == m.id,
        LoanApplication.status.in_(("draft", "submitted"))))
    if open_count >= MAX_OPEN_APPLICATIONS:
        raise HTTPException(409, "You already have applications being looked at. Please wait for those first.")
    nominated = []
    for no in dict.fromkeys(x.strip().upper() for x in body.guarantors if x.strip()):
        g = s.scalar(select(Member).where(Member.institution_id == m.institution_id, Member.member_no == no))
        if g is None or g.id == m.id:
            raise HTTPException(422, f"{no} is not another member of your SACCO. Check the member number.")
        nominated.append(no)
    a = LoanApplication(institution_id=m.institution_id, member_id=m.id, product_id=p.id,
                        amount_cents=int(body.amount_kes * 100), term_months=body.term_months, purpose=body.purpose,
                        status="draft", source="member_app", nominated_guarantors=nominated,
                        prepared_by_user_id=None, created_at=utcnow())
    s.add(a)
    s.flush()
    audit.record(s, me.principal, "loan_application.create", "loan_application", a.id,
                 after={"source": "member_app", "product": p.code, "amount_cents": a.amount_cents,
                        "term_months": a.term_months, "nominated_guarantors": nominated})
    s.commit()
    return {"ref": f"SWZ-{a.id}", "status": "draft",
            "message": "Sent. A credit officer will check it and contact your guarantors."}
