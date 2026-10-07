"""Sawazi by Pesara — HTTP API.

Run:  uvicorn sawazi.api:app --reload
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import csv
import hmac
import io
import logging
import os
import secrets
import threading
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, auth, daraja, guarantors, sms, ussd
from .auth import API_KEY_ROLES, PERMISSIONS, ROLES, Principal, require
from .db import get_session, init_db
from .engine.checkoff import reconcile_checkoff
from .engine.collections import build_queue, portfolio_at_risk
from .engine import allocation, appraisal, exposure
from .engine.matching import allocate, run_matching
from .importers import sources
from .importers.common import norm_phone
from .models import (Allocation, AllocationRules, ApiKey, CoreGuarantee, Guarantee, LoanApplication, LoanDecision, LoanProduct, AuditEvent, ExceptionItem, Institution, Loan, Member, MpesaCallback,
                     Reminder, SmsMessage, SmsOptOut, SmsSettings, StaffSession, StaffUser, Transaction)

@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Sawazi by Pesara", version="0.1.0", lifespan=lifespan,
              description="Repayment matching, check-off reconciliation and collections for SACCOs and microfinance institutions.")

_match_locks: dict[int, threading.Lock] = {}
_match_locks_guard = threading.Lock()


MATCHING_LOCK_BASE = 7_340_000_000  # PostgreSQL advisory lock keys: base + institution id


@contextmanager
def matching_lock(s: Session, institution_id: int):
    """One allocating action per institution at a time (matching runs, C2B callbacks, clearing suspense),
    so a payment is never allocated twice and loan balances are never updated on stale figures.
    A thread lock covers this process; on PostgreSQL a transaction advisory lock covers every API worker.
    The advisory lock is released when the caller's transaction commits or rolls back."""
    with _match_locks_guard:
        lock = _match_locks.setdefault(institution_id, threading.Lock())
    with lock:
        if s.get_bind().dialect.name == "postgresql":
            s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MATCHING_LOCK_BASE + institution_id})
        yield


def _inst(s: Session, institution_id: int) -> Institution:
    inst = s.get(Institution, institution_id)
    if not inst:
        raise HTTPException(404, "institution not found")
    return inst


class InstitutionIn(BaseModel):
    name: str
    kind: str = "sacco"
    paybill: str | None = None


@app.post("/institutions", dependencies=[Depends(auth.platform_key)], tags=["platform"])
def create_institution(body: InstitutionIn, s: Session = Depends(get_session)):
    inst = Institution(**body.model_dump())
    s.add(inst)
    s.flush()
    audit.record(s, audit.platform(inst.id), "institution.create", "institution", inst.id, after=body.model_dump())
    s.commit()
    return {"id": inst.id, "name": inst.name}


# ---------------------------------------------------------------- staff login and users

class StaffIn(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(pattern="^(" + "|".join(ROLES) + ")$")
    password: str


class StaffPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    role: str | None = Field(default=None, pattern="^(" + "|".join(ROLES) + ")$")
    is_active: bool | None = None
    password: str | None = None  # admin reset


class LoginIn(BaseModel):
    email: str
    password: str


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


def _user_out(u: StaffUser) -> dict:
    return {"id": u.id, "institution_id": u.institution_id, "email": u.email, "name": u.name, "role": u.role,
            "is_active": u.is_active, "last_login_at": u.last_login_at}


def _user_state(u: StaffUser) -> dict:
    return {"email": u.email, "name": u.name, "role": u.role, "is_active": u.is_active}


def _create_user(s: Session, institution_id: int, body: StaffIn, who: Principal) -> StaffUser:
    auth.check_password_policy(body.password)
    email = body.email.strip().lower()
    if s.scalar(select(StaffUser.id).where(StaffUser.email == email)):
        raise HTTPException(409, "a user with this email already exists")
    u = StaffUser(institution_id=institution_id, email=email, name=body.name, role=body.role,
                  password_hash=auth.hash_password(body.password), is_active=True)
    s.add(u)
    s.flush()
    audit.record(s, who, "user.create", "staff_user", u.id, after=_user_state(u))
    s.commit()
    return u


@app.post("/institutions/{institution_id}/admin", dependencies=[Depends(auth.platform_key)], tags=["platform"])
def create_first_admin(institution_id: int, body: StaffIn, s: Session = Depends(get_session)):
    """Create an institution's first admin. After this, that admin manages their own staff."""
    _inst(s, institution_id)
    if body.role != "admin":
        raise HTTPException(422, "the first user must be an admin")
    return _user_out(_create_user(s, institution_id, body, audit.platform(institution_id)))


@app.post("/auth/login", tags=["auth"])
def login(body: LoginIn, request: Request, s: Session = Depends(get_session)):
    ip = request.client.host if request.client else None
    user = auth.authenticate(s, body.email, body.password)
    if not user:
        known = s.scalar(select(StaffUser).where(StaffUser.email == body.email.strip().lower()))
        if known:  # unknown emails have no institution to log against
            audit.record(s, audit.anonymous(known.institution_id), "auth.login_failed", "staff_user", known.id,
                         ip=ip, note="account deactivated" if not known.is_active else "wrong password")
            s.commit()
        raise HTTPException(401, "invalid email or password")
    token, expires = auth.issue_session(s, user)
    audit.record(s, Principal.of(user), "auth.login", "staff_user", user.id, ip=ip)
    s.commit()
    return {"token": token, "token_type": "bearer", "expires_at": expires, "user": _user_out(user)}


@app.post("/auth/logout", tags=["auth"])
def logout(sess: StaffSession = Depends(auth.current_session), user: StaffUser = Depends(auth.current_user),
           s: Session = Depends(get_session)):
    sess.revoked_at = auth.utcnow()
    audit.record(s, Principal.of(user), "auth.logout", "staff_user", user.id)
    s.commit()
    return {"logged_out": True}


@app.get("/auth/me", tags=["auth"])
def me(user: StaffUser = Depends(auth.current_user), s: Session = Depends(get_session)):
    """Who is logged in, their institution, and what their role allows (the console hides the rest)."""
    inst = s.get(Institution, user.institution_id)
    return {**_user_out(user), "institution_name": inst.name if inst else None,
            "permissions": sorted(a for a, roles in PERMISSIONS.items() if user.role in roles)}


@app.post("/auth/password", tags=["auth"])
def change_password(body: PasswordIn, token: str = Depends(auth.bearer_token),
                    user: StaffUser = Depends(auth.current_user), s: Session = Depends(get_session)):
    if not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(401, "current password is wrong")
    auth.check_password_policy(body.new_password)
    user.password_hash = auth.hash_password(body.new_password)
    auth.revoke_sessions(s, user.id, except_token=token)
    audit.record(s, Principal.of(user), "auth.password_change", "staff_user", user.id)
    s.commit()
    return {"changed": True}


@app.get("/institutions/{institution_id}/users", dependencies=[Depends(require("manage_users"))], tags=["users"])
def list_users(institution_id: int, s: Session = Depends(get_session)):
    users = s.scalars(select(StaffUser).where(StaffUser.institution_id == institution_id).order_by(StaffUser.name))
    return [_user_out(u) for u in users]


@app.post("/institutions/{institution_id}/users", tags=["users"])
def create_user(institution_id: int, body: StaffIn, s: Session = Depends(get_session),
                admin: Principal = Depends(require("manage_users"))):
    return _user_out(_create_user(s, institution_id, body, admin))


@app.patch("/institutions/{institution_id}/users/{user_id}", tags=["users"])
def update_user(institution_id: int, user_id: int, body: StaffPatch, s: Session = Depends(get_session),
                admin: Principal = Depends(require("manage_users"))):
    u = s.get(StaffUser, user_id)
    if not u or u.institution_id != institution_id:
        raise HTTPException(404, "user not found")
    if u.id == admin.id and ((body.role and body.role != "admin") or body.is_active is False):
        raise HTTPException(409, "you cannot remove your own admin access; ask another admin")
    before = _user_state(u)
    if body.name is not None:
        u.name = body.name
    if body.role is not None:
        u.role = body.role
    if body.password is not None:
        auth.check_password_policy(body.password)
        u.password_hash = auth.hash_password(body.password)
    if body.is_active is not None:
        u.is_active = body.is_active
    if body.is_active is False or body.password is not None or body.role is not None:
        auth.revoke_sessions(s, u.id)  # changes take effect now, not at token expiry
    after = _user_state(u)
    changed = [k for k in after if after[k] != before[k]]
    audit.record(s, admin, "user.update", "staff_user", u.id,
                 before={k: before[k] for k in changed},
                 after={**{k: after[k] for k in changed}, **({"password_reset": True} if body.password else {})})
    s.commit()
    return _user_out(u)


# ---------------------------------------------------------------- institution API keys

class ApiKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="what uses it, e.g. 'core banking nightly sync'")
    role: str = Field(pattern="^(" + "|".join(API_KEY_ROLES) + ")$")


def _key_out(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "role": k.role, "prefix": k.prefix, "created_at": k.created_at,
            "created_by_user_id": k.created_by_user_id, "last_used_at": k.last_used_at, "revoked_at": k.revoked_at}


@app.get("/institutions/{institution_id}/api-keys", dependencies=[Depends(require("manage_users"))], tags=["api keys"])
def list_api_keys(institution_id: int, s: Session = Depends(get_session)):
    keys = s.scalars(select(ApiKey).where(ApiKey.institution_id == institution_id).order_by(ApiKey.created_at.desc()))
    return [_key_out(k) for k in keys]


@app.post("/institutions/{institution_id}/api-keys", tags=["api keys"])
def create_api_key(institution_id: int, body: ApiKeyIn, s: Session = Depends(get_session),
                   admin: Principal = Depends(require("manage_users"))):
    """The full key is returned once, here. Sawazi only keeps its hash, so store it safely now."""
    raw = auth.new_api_key()
    k = ApiKey(institution_id=institution_id, name=body.name, role=body.role, prefix=raw[:12],
               key_hash=auth.token_hash(raw), created_by_user_id=admin.id, created_at=auth.utcnow())
    s.add(k)
    s.flush()
    audit.record(s, admin, "api_key.create", "api_key", k.id, after={"name": k.name, "role": k.role, "prefix": k.prefix})
    s.commit()
    return {**_key_out(k), "key": raw}


@app.delete("/institutions/{institution_id}/api-keys/{key_id}", tags=["api keys"])
def revoke_api_key(institution_id: int, key_id: int, s: Session = Depends(get_session),
                   admin: Principal = Depends(require("manage_users"))):
    """Revoke at once. The record is kept so past use stays traceable."""
    k = s.get(ApiKey, key_id)
    if not k or k.institution_id != institution_id:
        raise HTTPException(404, "API key not found")
    if not k.revoked_at:
        k.revoked_at = auth.utcnow()
        audit.record(s, admin, "api_key.revoke", "api_key", k.id, before={"revoked": False},
                     after={"revoked": True, "name": k.name, "prefix": k.prefix})
        s.commit()
    return _key_out(k)


IMPORTERS = {
    "members": sources.import_members,
    "loans": sources.import_loans,
    "mpesa": sources.import_mpesa_statement,
    "bank": sources.import_bank_statement,
    "member_balances": sources.import_member_balances,
}


def _summary(result: dict) -> dict:
    """Top-level counts and totals of an engine result, without per-line detail."""
    return {k: v for k, v in result.items() if isinstance(v, (str, int, float, bool)) or v is None}


def _audited(s: Session, who: Principal, action: str, result: dict, note: str | None = None) -> dict:
    """Engine runs commit their own work; record the run straight after."""
    audit.record(s, who, action, after=_summary(result), note=note)
    s.commit()
    return result


@app.post("/institutions/{institution_id}/import/{kind}")
async def import_file(
    institution_id: int,
    kind: str,
    file: UploadFile = File(...),
    employer: str | None = Query(None, description="check-off imports only"),
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$", description="YYYY-MM, check-off imports only"),
    replace: bool = Query(False, description="core_guarantees only: the file is the complete current list"),
    s: Session = Depends(get_session),
    who: Principal = Depends(require("reconcile")),
):
    _inst(s, institution_id)
    content = await file.read()
    note = f"file {file.filename}"
    if kind == "core_guarantees":
        result = sources.import_core_guarantees(s, institution_id, content, replace=replace).as_dict()
        return _audited(s, who, "import.core_guarantees", result, note + (", complete list" if replace else ""))
    if kind in IMPORTERS:
        result = IMPORTERS[kind](s, institution_id, content).as_dict()
        if kind == "loans":  # loans the core system now shows as repaid free their guarantors
            result["guarantees_released"] = guarantors.release_repaid(s, institution_id)
        return _audited(s, who, f"import.{kind}", result, note)
    if kind in {"checkoff_schedule", "checkoff_remittance"}:
        if not employer or not period:
            raise HTTPException(422, "employer and period are required for check-off imports")
        fn = sources.import_checkoff_schedule if kind == "checkoff_schedule" else sources.import_checkoff_remittance
        return _audited(s, who, f"import.{kind}", fn(s, institution_id, employer, period, content).as_dict(),
                        f"{note}, {employer} {period}")
    raise HTTPException(404, f"unknown import kind '{kind}'")


@app.post("/institutions/{institution_id}/checkoff/reconcile")
def checkoff(institution_id: int, employer: str, period: str, s: Session = Depends(get_session),
             who: Principal = Depends(require("reconcile"))):
    _inst(s, institution_id)
    return _audited(s, who, "checkoff.reconcile", reconcile_checkoff(s, institution_id, employer, period),
                    f"{employer} {period}")


@app.post("/institutions/{institution_id}/match")
def match(institution_id: int, s: Session = Depends(get_session), who: Principal = Depends(require("reconcile"))):
    _inst(s, institution_id)
    with matching_lock(s, institution_id):
        result = run_matching(s, institution_id)
    result["guarantees_released"] = guarantors.release_repaid(s, institution_id)
    return _audited(s, who, "match.run", result)


@app.post("/institutions/{institution_id}/collections/queue")
def collections_queue(institution_id: int, s: Session = Depends(get_session),
                      who: Principal = Depends(require("collections"))):
    _inst(s, institution_id)
    return _audited(s, who, "collections.queue", build_queue(s, institution_id))


@app.get("/institutions/{institution_id}/reminders", dependencies=[Depends(require("read"))])
def reminders(institution_id: int, limit: int = Query(50, ge=1, le=500),
              status: str = Query("queued", pattern="^(queued|failed)$"), s: Session = Depends(get_session)):
    rows = s.execute(
        select(Reminder, Loan, Member)
        .join(Loan, Reminder.loan_id == Loan.id)
        .join(Member, Loan.member_id == Member.id)
        .where(Reminder.institution_id == institution_id, Reminder.status == status)
        .order_by(Reminder.priority_score.desc())
        .limit(limit)
    ).all()
    return [
        {
            "id": r.id,
            "priority": r.priority_score,
            "channel": r.channel,
            "member_no": m.member_no,
            "member": m.name,
            "phone": m.phone,
            "loan_no": ln.loan_no,
            "days_in_arrears": ln.days_in_arrears,
            "arrears_kes": ln.arrears_cents / 100,
            "message": r.message,
            "status": r.status,
            "sms_allowed": r.channel in SENDABLE_CHANNELS,
        }
        for r, ln, m in rows
    ]


@app.get("/institutions/{institution_id}/exceptions", dependencies=[Depends(require("read"))])
def exceptions(institution_id: int, status: str = "open", kind: str | None = None, s: Session = Depends(get_session)):
    q = select(ExceptionItem).where(ExceptionItem.institution_id == institution_id, ExceptionItem.status == status)
    if kind:
        q = q.where(ExceptionItem.kind == kind)
    sev = {"high": 0, "medium": 1, "low": 2}
    items = sorted(s.scalars(q), key=lambda e: (sev.get(e.severity, 3), -e.amount_cents))
    txns = {t.id: t for t in s.scalars(select(Transaction).where(
        Transaction.institution_id == institution_id,
        Transaction.id.in_({e.transaction_id for e in items if e.transaction_id})))}
    members = {m.id: m for m in s.scalars(select(Member).where(
        Member.institution_id == institution_id, Member.id.in_({e.member_id for e in items if e.member_id})))}

    def txn(t: Transaction | None):
        return t and {"source": t.source, "reference": t.reference, "txn_time": t.txn_time,
                      "amount_kes": t.amount_cents / 100, "payer_name": t.payer_name, "payer_phone": t.payer_phone,
                      "account_ref": t.account_ref, "narrative": t.narrative, "status": t.status}

    def member(m: Member | None):
        return m and {"member_no": m.member_no, "name": m.name, "phone": m.phone}

    return [
        {"id": e.id, "kind": e.kind, "severity": e.severity, "amount_kes": e.amount_cents / 100,
         "detail": e.detail, "transaction_id": e.transaction_id, "member_id": e.member_id,
         "created_at": e.created_at, "transaction": txn(txns.get(e.transaction_id)),
         "suggested_member": member(members.get(e.member_id))}
        for e in items
    ]


@app.get("/institutions/{institution_id}/members", dependencies=[Depends(require("read"))])
def search_members(institution_id: int, q: str = Query(..., min_length=2, max_length=60),
                   limit: int = Query(20, ge=1, le=50), s: Session = Depends(get_session)):
    """Find a member by number, name, phone or ID number, with their loans (for clearing suspense)."""
    like = f"%{q.strip()}%"
    phone = norm_phone(q)
    found = list(s.scalars(select(Member).where(Member.institution_id == institution_id, or_(
        Member.member_no.ilike(like), Member.name.ilike(like), Member.id_number == q.strip(),
        Member.phone == phone if phone else Member.phone.ilike(like))).order_by(Member.member_no).limit(limit)))
    loans = defaultdict(list)
    for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id,
                                           Loan.member_id.in_([m.id for m in found]), Loan.status == "active")):
        loans[ln.member_id].append({"loan_no": ln.loan_no, "product": ln.product, "balance_kes": ln.balance_cents / 100,
                                    "arrears_kes": ln.arrears_cents / 100, "days_in_arrears": ln.days_in_arrears})
    return [{"member_no": m.member_no, "name": m.name, "phone": m.phone, "employer": m.employer,
             "loans": loans[m.id]} for m in found]


class ResolveIn(BaseModel):
    member_no: str | None = None  # for suspense: which member the money belongs to
    note: str | None = None


@app.post("/exceptions/{exception_id}/resolve")
def resolve(exception_id: int, body: ResolveIn, s: Session = Depends(get_session),
            user: Principal = Depends(require("resolve"))):
    # Under the institution's lock, and re-read inside it: two staff clearing the same item at once
    # get one allocation, and the loans are never updated on stale balances.
    with matching_lock(s, user.institution_id):
        s.expire_all()
        return _resolve(exception_id, body, s, user)


def _resolve(exception_id: int, body: ResolveIn, s: Session, user: Principal):
    e = s.get(ExceptionItem, exception_id)
    if not e or e.status != "open" or e.institution_id != user.institution_id:
        raise HTTPException(404, "open exception not found")
    before = {"status": e.status, "kind": e.kind, "amount_cents": e.amount_cents, "member_id": e.member_id}
    if e.kind == "suspense":
        if not body.member_no:
            raise HTTPException(422, "member_no is required to clear a suspense item")
        m = s.scalar(select(Member).where(Member.institution_id == e.institution_id, Member.member_no == body.member_no))
        if not m:
            raise HTTPException(404, "member not found")
        t = s.get(Transaction, e.transaction_id)
        before |= {"transaction_status": t.status, "transaction_member_id": t.member_id, "reference": t.reference}
        t.match_method, t.match_confidence = "manual", 100
        allocs = allocate(s, t, m.id)
        e.detail += f" | Cleared to {m.member_no}" + (f": {body.note}" if body.note else "")
        e.status = "resolved"
        guarantors.release_repaid(s, e.institution_id)  # this payment may have finished a loan
        audit.record(s, user, "suspense.clear", "exception", e.id, before=before, note=body.note, after={
            "status": e.status, "transaction_status": t.status, "member_id": m.id, "member_no": m.member_no,
            "allocations": [{"target": a.target, "loan_id": a.loan_id, "amount_cents": a.amount_cents} for a in allocs],
        })
        s.commit()
        return {"resolved": True, "allocations": [{"target": a.target, "amount_kes": a.amount_cents / 100} for a in allocs]}
    e.status = "resolved"
    if body.note:
        e.detail += f" | {body.note}"
    audit.record(s, user, "exception.resolve", "exception", e.id, before=before, after={"status": e.status},
                 note=body.note)
    s.commit()
    return {"resolved": True}


@app.get("/institutions/{institution_id}/dashboard", dependencies=[Depends(require("read"))])
def dashboard(institution_id: int, s: Session = Depends(get_session)):
    inst = _inst(s, institution_id)
    tx = defaultdict(lambda: {"count": 0, "kes": 0.0})
    for status, source, n, total in s.execute(
        select(Transaction.status, Transaction.source, func.count(), func.sum(Transaction.amount_cents))
        .where(Transaction.institution_id == institution_id)
        .group_by(Transaction.status, Transaction.source)
    ):
        tx[status]["count"] += n
        tx[status]["kes"] += int(total or 0) / 100
        tx[f"{source}_{status}"] = {"count": n, "kes": int(total or 0) / 100}
    exc = defaultdict(lambda: {"count": 0, "kes": 0.0})
    for kind, n, total in s.execute(
        select(ExceptionItem.kind, func.count(), func.sum(ExceptionItem.amount_cents))
        .where(ExceptionItem.institution_id == institution_id, ExceptionItem.status == "open")
        .group_by(ExceptionItem.kind)
    ):
        exc[kind] = {"count": n, "kes": int(total or 0) / 100}
    processed = tx["allocated"]["count"] + tx["suspense"]["count"]
    return {
        "institution": inst.name,
        "transactions": dict(tx),
        "auto_match_rate": round(100 * tx["allocated"]["count"] / processed, 1) if processed else None,
        "open_exceptions": dict(exc),
        "portfolio": portfolio_at_risk(s, institution_id),
    }


@app.get("/institutions/{institution_id}/exports/postings.csv")
def postings(institution_id: int, s: Session = Depends(get_session), who: Principal = Depends(require("export"))):
    """Allocations in a flat file the core banking system can import."""
    rows = s.execute(
        select(Allocation, Transaction, Member, Loan)
        .join(Transaction, Allocation.transaction_id == Transaction.id)
        .join(Member, Transaction.member_id == Member.id)
        .outerjoin(Loan, Allocation.loan_id == Loan.id)
        .where(Allocation.institution_id == institution_id)
        .order_by(Transaction.txn_time)
    ).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "source", "reference", "member_no", "member_name", "target", "loan_no", "amount"])
    for a, t, m, ln in rows:
        w.writerow([t.txn_time.strftime("%Y-%m-%d %H:%M"), t.source, t.reference, m.member_no, m.name,
                    a.target, ln.loan_no if ln else "", f"{a.amount_cents / 100:.2f}"])
    buf.seek(0)
    # member-level money leaving the system: record who took it
    audit.record(s, who, "export.postings", after={"rows": len(rows)})
    s.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=sawazi_postings.csv"})


# ---------------------------------------------------------------- audit log

@app.get("/institutions/{institution_id}/audit", dependencies=[Depends(require("audit"))], tags=["audit"])
def audit_log(
    institution_id: int,
    action: str | None = Query(None, description="exact action, or a prefix ending in '.', e.g. 'user.'"),
    entity_type: str | None = None,
    entity_id: int | None = None,
    actor_kind: str | None = Query(None, pattern="^(user|api_key|platform|anonymous|provider|member|system)$"),
    actor_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before_id: int | None = Query(None, description="page backwards: pass the last id you received"),
    limit: int = Query(100, ge=1, le=500),
    s: Session = Depends(get_session),
):
    """Newest first. Read-only: there is no endpoint to change or delete an audit event."""
    q = select(AuditEvent).where(AuditEvent.institution_id == institution_id)
    if action:
        q = q.where(AuditEvent.action.startswith(action) if action.endswith(".") else AuditEvent.action == action)
    if entity_type:
        q = q.where(AuditEvent.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(AuditEvent.entity_id == entity_id)
    if actor_kind:
        q = q.where(AuditEvent.actor_kind == actor_kind)
    if actor_id is not None:
        q = q.where(AuditEvent.actor_id == actor_id)
    if since:
        q = q.where(AuditEvent.at >= since)
    if until:
        q = q.where(AuditEvent.at < until)
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    return [
        {"id": e.id, "at": e.at, "actor_kind": e.actor_kind, "actor_id": e.actor_id, "actor_name": e.actor_name,
         "action": e.action, "entity_type": e.entity_type, "entity_id": e.entity_id, "before": e.before,
         "after": e.after, "note": e.note, "ip": e.ip}
        for e in s.scalars(q.order_by(AuditEvent.id.desc()).limit(limit))
    ]


# ---------------------------------------------------------------- SMS to members (Taifa Mobile)

SENDABLE_CHANNELS = {"sms", "call", "guarantor_notice", "field_visit"}  # "recovery" text is an internal note
MIN_DAYS_BETWEEN_SMS = 3  # per loan: protects members from repeat messages when the queue is rebuilt
MAX_SEND_BATCH = 100
DELIVERED_OR_PENDING = ("pending", "sent", "unknown", "simulated", "delivered")


class SendIn(BaseModel):
    reminder_ids: list[int] = Field(min_length=1, max_length=MAX_SEND_BATCH)


def _sms_settings(s: Session, institution_id: int) -> SmsSettings:
    st = s.scalar(select(SmsSettings).where(SmsSettings.institution_id == institution_id))
    return st or SmsSettings(institution_id=institution_id, enabled=False)


def _opted_out(s: Session, institution_id: int, phone: str) -> bool:
    return s.scalar(select(SmsOptOut.id).where(SmsOptOut.institution_id == institution_id,
                                               SmsOptOut.phone == phone)) is not None


def _sms_out(m: SmsMessage) -> dict:
    return {"id": m.id, "reminder_id": m.reminder_id, "loan_id": m.loan_id, "member_id": m.member_id,
            "phone": m.phone, "body": m.body, "provider": m.provider, "status": m.status,
            "provider_message_id": m.provider_message_id, "provider_status": m.provider_status,
            "provider_description": m.provider_description, "approved_by_user_id": m.approved_by_user_id,
            "approved_at": m.approved_at, "sent_at": m.sent_at, "delivery_status": m.delivery_status,
            "delivered_at": m.delivered_at}


@app.post("/institutions/{institution_id}/reminders/send", tags=["sms"])
def send_reminders(institution_id: int, body: SendIn, s: Session = Depends(get_session),
                   who: Principal = Depends(require("send_sms")), provider=Depends(sms.get_provider)):
    """Staff approval: send these queued reminders to members by SMS, now. Each one is checked first
    (opt-out, valid phone, not messaged recently) and the result for every reminder is returned."""
    _inst(s, institution_id)
    settings = _sms_settings(s, institution_id)
    if provider.name != "simulate" and not settings.enabled:
        raise HTTPException(409, "SMS is not switched on for this institution yet (admin: SMS settings)")
    results = []
    for rid in dict.fromkeys(body.reminder_ids):  # de-duplicate, keep order
        results.append({"reminder_id": rid, **_send_one(s, institution_id, rid, who, provider, settings)})
    counts = defaultdict(int)
    for r in results:
        counts[r["result"]] += 1
    return {"results": results, "counts": dict(counts), "provider": provider.name}


def _send_one(s: Session, institution_id: int, rid: int, who: Principal, provider, settings: SmsSettings) -> dict:
    r = s.get(Reminder, rid)
    if not r or r.institution_id != institution_id:
        return {"result": "skipped", "reason": "reminder not found"}
    if r.status not in ("queued", "failed"):
        return {"result": "skipped", "reason": f"already {r.status}"}
    if r.channel not in SENDABLE_CHANNELS:
        return {"result": "skipped", "reason": "this is an internal recovery note, not a message for the member"}
    loan = s.get(Loan, r.loan_id)
    member = s.get(Member, loan.member_id)
    phone = norm_phone(member.phone)
    if not phone:
        return {"result": "skipped", "reason": "member has no valid phone number"}
    if _opted_out(s, institution_id, phone):
        return {"result": "skipped", "reason": "member has opted out of SMS"}
    since = auth.utcnow() - timedelta(days=MIN_DAYS_BETWEEN_SMS)
    recent = s.scalar(select(SmsMessage.approved_at).where(
        SmsMessage.institution_id == institution_id, SmsMessage.loan_id == loan.id,
        SmsMessage.status.in_(DELIVERED_OR_PENDING), SmsMessage.approved_at >= since))
    if recent:
        return {"result": "skipped", "reason": f"an SMS for this loan already went out on {recent:%Y-%m-%d %H:%M}"}

    # Claim the reminder atomically, so a double click or two staff at once can't send it twice.
    claimed = s.execute(update(Reminder).where(Reminder.id == rid, Reminder.status.in_(("queued", "failed")))
                        .values(status="sending")).rowcount
    if claimed != 1:
        s.rollback()
        return {"result": "skipped", "reason": "already being sent"}
    text = r.message + (f" {settings.opt_out_text}" if settings.opt_out_text else "")
    msg = SmsMessage(institution_id=institution_id, reminder_id=rid, loan_id=loan.id, member_id=member.id,
                     phone=phone, body=text, provider=provider.name, status="pending",
                     approved_by_user_id=who.id, approved_at=auth.utcnow())
    s.add(msg)
    s.flush()
    audit.record(s, who, "sms.approve", "sms_message", msg.id, before={"reminder_status": "queued"},
                 after={"reminder_id": rid, "loan_no": loan.loan_no, "member_no": member.member_no,
                        "phone": phone, "provider": provider.name})
    s.commit()  # the approval is on record before anything leaves the building

    try:
        res = provider.send(phone, text, settings.service_name)
    except Exception as e:  # never retry blind: it may have gone out
        res = sms.SendResult("unknown", description=f"Error while sending ({type(e).__name__}); check before resending")
    msg.status, msg.provider_message_id = res.status, res.provider_message_id
    msg.provider_status, msg.provider_description = res.provider_status, res.description
    msg.sent_at = auth.utcnow() if res.status != "failed" else None
    s.execute(update(Reminder).where(Reminder.id == rid)
              .values(status="failed" if res.status == "failed" else "sent"))
    s.commit()
    return {"result": res.status, "sms_id": msg.id, "phone": phone, "detail": res.description}


@app.get("/institutions/{institution_id}/sms", dependencies=[Depends(require("read"))], tags=["sms"])
def list_sms(institution_id: int, status: str | None = None, limit: int = Query(100, ge=1, le=500),
             s: Session = Depends(get_session)):
    q = select(SmsMessage).where(SmsMessage.institution_id == institution_id)
    if status:
        q = q.where(SmsMessage.status == status)
    return [_sms_out(m) for m in s.scalars(q.order_by(SmsMessage.id.desc()).limit(limit))]


class OptOutIn(BaseModel):
    phone: str
    note: str | None = None


def _add_opt_out(s: Session, institution_id: int, phone: str, source: str, note: str | None) -> SmsOptOut | None:
    """Idempotent. Returns the new opt-out, or None if the number was already opted out."""
    if _opted_out(s, institution_id, phone):
        return None
    o = SmsOptOut(institution_id=institution_id, phone=phone, source=source, note=note, created_at=auth.utcnow())
    s.add(o)
    s.flush()
    return o


@app.get("/institutions/{institution_id}/sms/opt-outs", dependencies=[Depends(require("read"))], tags=["sms"])
def list_opt_outs(institution_id: int, s: Session = Depends(get_session)):
    rows = s.scalars(select(SmsOptOut).where(SmsOptOut.institution_id == institution_id).order_by(SmsOptOut.id))
    return [{"phone": o.phone, "source": o.source, "note": o.note, "created_at": o.created_at} for o in rows]


@app.post("/institutions/{institution_id}/sms/opt-outs", tags=["sms"])
def add_opt_out(institution_id: int, body: OptOutIn, s: Session = Depends(get_session),
                who: Principal = Depends(require("send_sms"))):
    """Record a member's request to stop SMS (e.g. they called in)."""
    phone = norm_phone(body.phone)
    if not phone:
        raise HTTPException(422, "not a valid Kenyan phone number")
    o = _add_opt_out(s, institution_id, phone, "staff", body.note)
    if o:
        audit.record(s, who, "sms.opt_out", "sms_opt_out", o.id, after={"phone": phone, "source": "staff"},
                     note=body.note)
        s.commit()
    return {"phone": phone, "opted_out": True, "already": o is None}


@app.delete("/institutions/{institution_id}/sms/opt-outs/{phone}", tags=["sms"])
def remove_opt_out(institution_id: int, phone: str,
                   note: str = Query(..., min_length=3, description="why, e.g. 'member asked in branch'"),
                   s: Session = Depends(get_session), who: Principal = Depends(require("sms_settings"))):
    """Opt a number back in. Only on the member's own request; the reason is recorded."""
    o = s.scalar(select(SmsOptOut).where(SmsOptOut.institution_id == institution_id,
                                         SmsOptOut.phone == norm_phone(phone)))
    if not o:
        raise HTTPException(404, "this number is not opted out")
    audit.record(s, who, "sms.opt_in", "sms_opt_out", o.id, before={"phone": o.phone, "source": o.source},
                 after={"phone": o.phone, "opted_out": False}, note=note)
    s.delete(o)
    s.commit()
    return {"phone": o.phone, "opted_out": False}


class SmsSettingsIn(BaseModel):
    enabled: bool
    service_name: str | None = Field(default=None, max_length=100)
    opt_out_text: str | None = Field(default=None, max_length=160)


def _settings_out(st: SmsSettings) -> dict:
    return {"enabled": bool(st.enabled), "service_name": st.service_name, "opt_out_text": st.opt_out_text}


@app.get("/institutions/{institution_id}/sms/settings", dependencies=[Depends(require("sms_settings"))], tags=["sms"])
def get_sms_settings(institution_id: int, s: Session = Depends(get_session)):
    return _settings_out(_sms_settings(s, institution_id))


@app.put("/institutions/{institution_id}/sms/settings", tags=["sms"])
def put_sms_settings(institution_id: int, body: SmsSettingsIn, s: Session = Depends(get_session),
                     who: Principal = Depends(require("sms_settings"))):
    st = _sms_settings(s, institution_id)
    before = _settings_out(st) if st.id else None
    st.enabled, st.service_name, st.opt_out_text = body.enabled, body.service_name, body.opt_out_text
    s.add(st)
    s.flush()
    audit.record(s, who, "sms.settings", "sms_settings", st.id, before=before, after=_settings_out(st))
    s.commit()
    return _settings_out(st)


# ---------------------------------------------------------------- Taifa Mobile callbacks
# Taifa does not sign callbacks, so the URL carries a secret (SAWAZI_SMS_CALLBACK_TOKEN).
# Register https://<host>/callbacks/taifa/<token>/{delivery,subscription,incoming} with Taifa Mobile,
# and allow only Taifa's IP addresses at the reverse proxy if they publish them.

def _callback_token(token: str) -> None:
    expected = os.getenv("SAWAZI_SMS_CALLBACK_TOKEN")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(404, "not found")


CALLBACK = Principal("provider", None, 0, "", "Taifa Mobile callback")


def _institutions_for(s: Session, service: dict | None, phone: str) -> list[int]:
    """Which institutions a member's STOP applies to: the one owning the service, otherwise every
    institution that has messaged this number (when in doubt, respect the opt-out more widely)."""
    name = (service or {}).get("service_name") if isinstance(service, dict) else None
    if name:
        ids = list(s.scalars(select(SmsSettings.institution_id).where(SmsSettings.service_name == name)))
        if ids:
            return ids
    return list(s.scalars(select(SmsMessage.institution_id).where(SmsMessage.phone == phone).distinct()))


def _member_opt_out(s: Session, service: dict | None, phone: str | None, source: str, note: str) -> int:
    phone = norm_phone(phone)
    if not phone:
        return 0
    n = 0
    for iid in _institutions_for(s, service, phone):
        o = _add_opt_out(s, iid, phone, source, note)
        if o:
            audit.record(s, replace(CALLBACK, institution_id=iid), "sms.opt_out", "sms_opt_out", o.id,
                         after={"phone": phone, "source": source}, note=note)
            n += 1
    s.commit()
    return n


@app.post("/callbacks/taifa/{token}/delivery", tags=["callbacks"], include_in_schema=False)
def taifa_delivery(token: str, body: dict, s: Session = Depends(get_session)):
    _callback_token(token)
    msg = s.scalar(select(SmsMessage).where(SmsMessage.provider == "taifa",
                                            SmsMessage.provider_message_id == str(body.get("messageId"))))
    if not msg:
        return {"ok": True, "matched": False}  # 200 so Taifa does not keep retrying
    status = str(body.get("status", ""))
    msg.delivery_status = status[:100]
    if status in sms.DELIVERED:
        msg.status, msg.delivered_at = "delivered", auth.utcnow()
    else:
        msg.status = "undelivered"
    s.commit()
    if status == sms.SENDER_BLOCKED:
        _member_opt_out(s, None, msg.phone, "sender_blocked", "member blocked our sender ID")
    return {"ok": True, "matched": True}


@app.post("/callbacks/taifa/{token}/subscription", tags=["callbacks"], include_in_schema=False)
def taifa_subscription(token: str, body: dict, s: Session = Depends(get_session)):
    _callback_token(token)
    if str(body.get("update_description", "")).upper() == "DEACTIVATION":
        n = _member_opt_out(s, body.get("service"), body.get("phone_number"), "subscription",
                            "member unsubscribed from the service")
        return {"ok": True, "opted_out": n}
    return {"ok": True, "opted_out": 0}


@app.post("/callbacks/taifa/{token}/incoming", tags=["callbacks"], include_in_schema=False)
def taifa_incoming(token: str, body: dict, s: Session = Depends(get_session)):
    _callback_token(token)
    if sms.is_stop(body.get("message")):
        n = _member_opt_out(s, body.get("service"), body.get("phone_number"), "member_sms",
                            f"member replied: {str(body.get('message'))[:50]}")
        return {"ok": True, "opted_out": n}
    return {"ok": True, "opted_out": 0}  # other replies are not handled yet


# ---------------------------------------------------------------- M-Pesa Daraja C2B callbacks
# Register with scripts/daraja_register.py:
#   https://<host>/callbacks/c2b/<SAWAZI_DARAJA_CALLBACK_TOKEN>/confirmation  (and /validation)
# Safaricom does not sign callbacks. Defences: secret URL token, optional IP allowlist
# (SAWAZI_DARAJA_ALLOWED_IPS), and every callback is confirmed against the next paybill statement.

log = logging.getLogger("sawazi.c2b")


def _c2b_guard(token: str, request: Request) -> str | None:
    expected = os.getenv("SAWAZI_DARAJA_CALLBACK_TOKEN")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(404, "not found")
    ip = request.client.host if request.client else None
    allowed = {x.strip() for x in os.getenv("SAWAZI_DARAJA_ALLOWED_IPS", "").split(",") if x.strip()}
    if allowed and ip not in allowed:
        log.warning("C2B callback from %s refused (not in SAWAZI_DARAJA_ALLOWED_IPS)", ip)
        raise HTTPException(404, "not found")
    return ip


def _institution_for_shortcode(s: Session, shortcode: str) -> Institution | None:
    found = list(s.scalars(select(Institution).where(Institution.paybill == shortcode)))
    if len(found) != 1:  # unknown or ambiguous: never guess whose money it is
        log.warning("C2B for paybill %s matched %d institutions; not recorded (the statement will catch it)",
                    shortcode, len(found))
        return None
    return found[0]


@app.post("/callbacks/c2b/{token}/validation", tags=["callbacks"], include_in_schema=False)
def c2b_validation(token: str, body: dict, request: Request, s: Session = Depends(get_session)):
    """Always accepts. Sawazi never blocks a member's payment."""
    ip = _c2b_guard(token, request)
    try:
        p = daraja.parse(body)
    except daraja.BadCallback as e:
        log.warning("C2B validation unreadable: %s", e)
        return daraja.ACCEPT
    inst = _institution_for_shortcode(s, p.shortcode)
    if inst:
        s.add(MpesaCallback(institution_id=inst.id, kind="validation", trans_id=p.trans_id,
                            amount_cents=p.amount_cents, payload=body, received_at=auth.utcnow(), ip=ip))
        s.commit()
    return daraja.ACCEPT


@app.post("/callbacks/c2b/{token}/confirmation", tags=["callbacks"], include_in_schema=False)
def c2b_confirmation(token: str, body: dict, request: Request, s: Session = Depends(get_session)):
    """Record the payment (idempotent on TransID, shared with statement imports) and match it now."""
    ip = _c2b_guard(token, request)
    try:
        p = daraja.parse(body)
    except daraja.BadCallback as e:
        log.warning("C2B confirmation unreadable: %s", e)
        return daraja.ACCEPT  # acknowledge anyway; the statement is the fallback
    inst = _institution_for_shortcode(s, p.shortcode)
    if not inst:
        return daraja.ACCEPT
    t = s.scalar(select(Transaction).where(Transaction.institution_id == inst.id, Transaction.source == "mpesa",
                                           Transaction.reference == p.trans_id))
    existing = t is not None
    if t is None:
        t = Transaction(institution_id=inst.id, source="mpesa", reference=p.trans_id, txn_time=p.txn_time,
                        amount_cents=p.amount_cents, payer_name=p.payer_name, payer_phone=p.payer_phone,
                        account_ref=p.account_ref, narrative=p.transaction_type)
        try:
            with s.begin_nested():  # Safaricom can send the same callback twice at the same moment
                s.add(t)
        except IntegrityError:
            t = s.scalar(select(Transaction).where(Transaction.institution_id == inst.id,
                                                   Transaction.source == "mpesa", Transaction.reference == p.trans_id))
            existing = True
    elif t.amount_cents != p.amount_cents:  # repeat callback that disagrees with what we hold
        s.add(ExceptionItem(institution_id=inst.id, kind="c2b_mismatch", severity="high", transaction_id=t.id,
                            amount_cents=p.amount_cents,
                            detail=f"M-Pesa {p.trans_id}: callback says KES {p.amount_cents / 100:,.2f}, "
                                   f"we hold KES {t.amount_cents / 100:,.2f}. Check the paybill statement."))
    confirmed_at = None
    if existing and t.amount_cents == p.amount_cents:
        earlier = list(s.scalars(select(MpesaCallback.statement_confirmed_at).where(
            MpesaCallback.institution_id == inst.id, MpesaCallback.kind == "confirmation",
            MpesaCallback.trans_id == p.trans_id)))
        if not earlier:  # the payment came from a statement upload: already confirmed
            confirmed_at = auth.utcnow()
        elif all(earlier):
            confirmed_at = earlier[0]
    s.add(MpesaCallback(institution_id=inst.id, kind="confirmation", trans_id=p.trans_id, amount_cents=p.amount_cents,
                        payload=body, received_at=auth.utcnow(), ip=ip, transaction_id=t.id,
                        statement_confirmed_at=confirmed_at))
    s.commit()
    try:
        with matching_lock(s, inst.id):
            run_matching(s, inst.id)
            guarantors.release_repaid(s, inst.id)
            s.commit()
    except Exception:  # the payment is stored; the next match run picks it up
        s.rollback()
        log.exception("C2B real-time matching failed for institution %s", inst.id)
    return daraja.ACCEPT


@app.get("/institutions/{institution_id}/c2b/unconfirmed", dependencies=[Depends(require("read"))], tags=["c2b"])
def c2b_unconfirmed(institution_id: int, older_than_hours: int = Query(48, ge=0),
                    s: Session = Depends(get_session)):
    """Payments that arrived by callback but are not yet on an uploaded paybill statement.
    After a day or two these deserve a look: a statement should always contain them."""
    cutoff = auth.utcnow() - timedelta(hours=older_than_hours)
    rows = s.scalars(select(MpesaCallback).where(
        MpesaCallback.institution_id == institution_id, MpesaCallback.kind == "confirmation",
        MpesaCallback.statement_confirmed_at.is_(None), MpesaCallback.received_at <= cutoff,
    ).order_by(MpesaCallback.received_at))
    return [{"trans_id": c.trans_id, "amount_kes": c.amount_cents / 100, "received_at": c.received_at,
             "transaction_id": c.transaction_id, "ip": c.ip} for c in rows]


# ---------------------------------------------------------------- allocation rules

class ExcessIn(BaseModel):
    target: str = Field(pattern="^(" + "|".join(allocation.EXCESS_TARGETS) + ")$")
    percent: int | None = Field(default=None, ge=1, le=99)
    max_kes: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)


class RulesIn(BaseModel):
    loan_order: str = Field(pattern="^(" + "|".join(allocation.LOAN_ORDERS) + ")$")
    arrears_order: list[str] = Field(min_length=3, max_length=3)
    pay_current_installment: bool = True
    excess: list[ExcessIn] = Field(min_length=1, max_length=len(allocation.EXCESS_TARGETS))

    def to_rules(self) -> allocation.Rules:
        r = allocation.Rules(
            loan_order=self.loan_order, arrears_order=list(self.arrears_order),
            pay_current_installment=self.pay_current_installment,
            excess=[allocation.ExcessBucket(b.target, b.percent,
                                            int(b.max_kes * 100) if b.max_kes is not None else None)
                    for b in self.excess])
        try:
            r.validate()
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return r


def _rules_out(s: Session, institution_id: int) -> dict:
    row = s.scalar(select(AllocationRules).where(AllocationRules.institution_id == institution_id))
    return {**allocation.rules_for(s, institution_id).as_dict(), "is_default": row is None,
            "updated_at": row.updated_at if row else None,
            "updated_by_user_id": row.updated_by_user_id if row else None,
            "choices": {"loan_order": allocation.LOAN_ORDERS, "arrears_parts": list(allocation.ARREARS_PARTS),
                        "excess_targets": list(allocation.EXCESS_TARGETS)}}


@app.get("/institutions/{institution_id}/allocation-rules", dependencies=[Depends(require("read"))],
         tags=["allocation"])
def get_allocation_rules(institution_id: int, s: Session = Depends(get_session)):
    return _rules_out(s, institution_id)


@app.put("/institutions/{institution_id}/allocation-rules", tags=["allocation"])
def put_allocation_rules(institution_id: int, body: RulesIn, s: Session = Depends(get_session),
                         who: Principal = Depends(require("allocation_rules"))):
    """Applies to payments allocated from now on. Past allocations are never changed."""
    _inst(s, institution_id)
    rules = body.to_rules()
    before = allocation.rules_for(s, institution_id).as_dict()
    row = s.scalar(select(AllocationRules).where(AllocationRules.institution_id == institution_id))
    if row is None:
        row = AllocationRules(institution_id=institution_id)
        s.add(row)
    row.loan_order, row.arrears_order = rules.loan_order, rules.arrears_order
    row.pay_current_installment = rules.pay_current_installment
    row.excess = [{"target": b.target, "percent": b.percent, "max_cents": b.max_cents} for b in rules.excess]
    row.updated_at, row.updated_by_user_id = auth.utcnow(), who.id
    s.flush()
    audit.record(s, who, "allocation_rules.update", "allocation_rules", row.id, before=before, after=rules.as_dict())
    s.commit()
    return _rules_out(s, institution_id)


class PreviewIn(BaseModel):
    member_no: str
    amount_kes: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    loan_no: str | None = Field(default=None, description="if the payment named a loan")
    rules: RulesIn | None = Field(default=None, description="draft rules to try; current rules if left out")


@app.post("/institutions/{institution_id}/allocation-rules/preview", dependencies=[Depends(require("read"))],
          tags=["allocation"])
def preview_allocation(institution_id: int, body: PreviewIn, s: Session = Depends(get_session)):
    """How a payment from this member would be split. Changes nothing."""
    m = s.scalar(select(Member).where(Member.institution_id == institution_id, Member.member_no == body.member_no))
    if not m:
        raise HTTPException(404, "member not found")
    rules = body.rules.to_rules() if body.rules else allocation.rules_for(s, institution_id)
    loans = list(s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.member_id == m.id,
                                              Loan.status == "active")))
    preferred = next((ln.id for ln in loans if body.loan_no and ln.loan_no == body.loan_no), None)
    states = [allocation.LoanState.of(ln) for ln in loans]  # copies: the real loans are not touched
    lines = allocation.plan(int(body.amount_kes * 100), states, rules, preferred)
    loan_no = {ln.id: ln.loan_no for ln in loans}

    def loan_view(x) -> dict:
        return {"loan_no": x.loan_no, "balance_kes": x.balance_cents / 100, "arrears_kes": x.arrears_cents / 100,
                "penalty_arrears_kes": x.penalty_arrears_cents / 100,
                "interest_arrears_kes": x.interest_arrears_cents / 100,
                "installment_kes": x.installment_cents / 100}

    return {
        "member_no": m.member_no, "member": m.name, "amount_kes": float(body.amount_kes),
        "rules": rules.as_dict(),
        "lines": [{"target": x.target, "loan_no": loan_no.get(x.loan_id), "amount_kes": x.amount_cents / 100}
                  for x in lines],
        "loans_before": [loan_view(allocation.LoanState.of(ln)) for ln in loans],
        "loans_after": [loan_view(x) for x in states],
    }


# ---------------------------------------------------------------- loan products and appraisal

def _cents(kes: Decimal) -> int:
    return int(kes * 100)


class ProductIn(BaseModel):
    code: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    active: bool = True
    min_amount_kes: Decimal = Field(default=Decimal(1000), gt=0, max_digits=14, decimal_places=2)
    max_amount_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    max_term_months: int = Field(ge=1, le=240)
    interest_rate_pct: Decimal = Field(ge=0, le=100, max_digits=5, decimal_places=2,
                                       description="yearly; only used to estimate the instalment")
    interest_method: str = Field(default="reducing", pattern="^(reducing|flat)$")
    deposits_multiplier: Decimal = Field(default=Decimal(3), ge=0, le=20, max_digits=4, decimal_places=2,
                                         description="0 switches the check off")
    min_membership_months: int = Field(default=6, ge=0, le=120)
    max_arrears_days: int = Field(default=30, ge=0, le=365)
    one_third_rule: bool = True
    guarantor_cover: str = Field(default="above_deposits", pattern="^(above_deposits|full|none)$")
    min_guarantors: int = Field(default=0, ge=0, le=10)
    second_approval_above_kes: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)

    def apply(self, prod: LoanProduct) -> None:
        if self.min_amount_kes > self.max_amount_kes:
            raise HTTPException(422, "minimum amount is more than the maximum")
        prod.code, prod.name, prod.active = self.code.upper(), self.name, self.active
        prod.min_amount_cents, prod.max_amount_cents = _cents(self.min_amount_kes), _cents(self.max_amount_kes)
        prod.max_term_months, prod.interest_method = self.max_term_months, self.interest_method
        prod.interest_rate_bps = int(self.interest_rate_pct * 100)
        prod.deposits_multiplier_pct = int(self.deposits_multiplier * 100)
        prod.min_membership_months, prod.max_arrears_days = self.min_membership_months, self.max_arrears_days
        prod.one_third_rule, prod.guarantor_cover = self.one_third_rule, self.guarantor_cover
        prod.min_guarantors = self.min_guarantors
        prod.second_approval_above_cents = (_cents(self.second_approval_above_kes)
                                            if self.second_approval_above_kes is not None else None)


def _product_out(p: LoanProduct) -> dict:
    return {"id": p.id, "code": p.code, "name": p.name, "active": p.active,
            "min_amount_kes": p.min_amount_cents / 100, "max_amount_kes": p.max_amount_cents / 100,
            "max_term_months": p.max_term_months, "interest_rate_pct": p.interest_rate_bps / 100,
            "interest_method": p.interest_method, "deposits_multiplier": p.deposits_multiplier_pct / 100,
            "min_membership_months": p.min_membership_months, "max_arrears_days": p.max_arrears_days,
            "one_third_rule": p.one_third_rule, "guarantor_cover": p.guarantor_cover,
            "min_guarantors": p.min_guarantors,
            "second_approval_above_kes": (p.second_approval_above_cents / 100
                                          if p.second_approval_above_cents is not None else None)}


def _product_rules(p: LoanProduct) -> appraisal.Product:
    return appraisal.Product(p.min_amount_cents, p.max_amount_cents, p.max_term_months, p.interest_rate_bps,
                             p.interest_method, p.deposits_multiplier_pct, p.min_membership_months,
                             p.max_arrears_days, p.one_third_rule, p.guarantor_cover, p.min_guarantors)


def _get_product(s: Session, institution_id: int, product_id: int) -> LoanProduct:
    p = s.get(LoanProduct, product_id)
    if not p or p.institution_id != institution_id:
        raise HTTPException(404, "loan product not found")
    return p


@app.get("/institutions/{institution_id}/loan-products", dependencies=[Depends(require("read"))], tags=["loans"])
def list_products(institution_id: int, s: Session = Depends(get_session)):
    rows = s.scalars(select(LoanProduct).where(LoanProduct.institution_id == institution_id).order_by(LoanProduct.code))
    return [_product_out(p) for p in rows]


@app.post("/institutions/{institution_id}/loan-products", tags=["loans"])
def create_product(institution_id: int, body: ProductIn, s: Session = Depends(get_session),
                   who: Principal = Depends(require("loan_products"))):
    _inst(s, institution_id)
    if s.scalar(select(LoanProduct.id).where(LoanProduct.institution_id == institution_id,
                                             LoanProduct.code == body.code.upper())):
        raise HTTPException(409, f"product code {body.code.upper()} already exists")
    now = auth.utcnow()
    p = LoanProduct(institution_id=institution_id, created_at=now, updated_at=now)
    body.apply(p)
    s.add(p)
    s.flush()
    audit.record(s, who, "loan_product.create", "loan_product", p.id, after=_product_out(p))
    s.commit()
    return _product_out(p)


@app.put("/institutions/{institution_id}/loan-products/{product_id}", tags=["loans"])
def update_product(institution_id: int, product_id: int, body: ProductIn, s: Session = Depends(get_session),
                   who: Principal = Depends(require("loan_products"))):
    """Changes apply to appraisals from now on; applications keep the appraisal they were given."""
    p = _get_product(s, institution_id, product_id)
    clash = s.scalar(select(LoanProduct.id).where(LoanProduct.institution_id == institution_id,
                                                  LoanProduct.code == body.code.upper(), LoanProduct.id != p.id))
    if clash:
        raise HTTPException(409, f"product code {body.code.upper()} already exists")
    before = _product_out(p)
    body.apply(p)
    p.updated_at = auth.utcnow()
    after = _product_out(p)
    audit.record(s, who, "loan_product.update", "loan_product", p.id,
                 before={k: v for k, v in before.items() if after[k] != v},
                 after={k: v for k, v in after.items() if before[k] != v})
    s.commit()
    return after


def _member_facts(s: Session, institution_id: int, member_no: str):
    m = s.scalar(select(Member).where(Member.institution_id == institution_id, Member.member_no == member_no))
    if not m:
        raise HTTPException(404, "member not found")
    loans = [appraisal.ExistingLoan(ln.loan_no, ln.balance_cents, ln.days_in_arrears)
             for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.member_id == m.id,
                                                    Loan.status == "active"))]
    return m, appraisal.MemberFacts(m.joined_on, m.deposits_cents, m.gross_pay_cents, m.net_pay_cents), loans


def _appraisal_out(a: appraisal.Appraisal) -> dict:
    return {"outcome": a.outcome, "instalment_kes": a.instalment_cents / 100,
            "max_eligible_kes": a.max_eligible_cents / 100, "max_eligible_partial": a.max_eligible_partial,
            "unknown_limits": [k for k, v in a.limits.items() if v is None],
            "required_cover_kes": a.required_cover_cents / 100, "accepted_cover_kes": a.accepted_cover_cents / 100,
            "checks": [{"code": c.code, "status": c.status, "message": c.message} for c in a.checks]}


class WhatIfIn(BaseModel):
    member_no: str
    product_id: int
    amount_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    term_months: int = Field(ge=1, le=240)


@app.post("/institutions/{institution_id}/appraisal/what-if", dependencies=[Depends(require("read"))], tags=["loans"])
def appraisal_what_if(institution_id: int, body: WhatIfIn, s: Session = Depends(get_session)):
    """Appraise a possible loan for a member without saving anything (no guarantors yet)."""
    p = _get_product(s, institution_id, body.product_id)
    m, facts, loans = _member_facts(s, institution_id, body.member_no)
    a = appraisal.appraise(_cents(body.amount_kes), body.term_months, _product_rules(p), facts, loans, [],
                           auth.utcnow().date())
    return {"member_no": m.member_no, "member": m.name, "product": p.code, **_appraisal_out(a)}


# ---------------------------------------------------------------- loan applications

class ApplicationIn(BaseModel):
    member_no: str
    product_id: int
    amount_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    term_months: int = Field(ge=1, le=240)
    purpose: str | None = Field(default=None, max_length=500)


class ApplicationPatch(BaseModel):
    amount_kes: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=2)
    term_months: int | None = Field(default=None, ge=1, le=240)
    purpose: str | None = Field(default=None, max_length=500)


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|decline)$")
    note: str | None = Field(default=None, max_length=1000)
    override_reason: str | None = Field(default=None, min_length=10, max_length=1000,
                                        description="required to approve when a check failed or is unknown")


def _get_application(s: Session, institution_id: int, app_id: int, lock: bool = False) -> LoanApplication:
    q = select(LoanApplication).where(LoanApplication.id == app_id, LoanApplication.institution_id == institution_id)
    a = s.scalar(q.with_for_update() if lock else q)
    if not a:
        raise HTTPException(404, "application not found")
    return a


def _appraise_application(s: Session, a: LoanApplication) -> appraisal.Appraisal:
    prod = s.get(LoanProduct, a.product_id)
    m = s.get(Member, a.member_id)
    _, facts, loans = _member_facts(s, a.institution_id, m.member_no)
    return appraisal.appraise(a.amount_cents, a.term_months, _product_rules(prod), facts, loans,
                              _guarantees_for(s, a), auth.utcnow().date())


def _guarantees_for(s: Session, a: LoanApplication) -> list:
    rows = s.scalars(select(Guarantee).where(Guarantee.application_id == a.id,
                                             Guarantee.status.in_(("requested", "accepted", "declined"))))
    return [appraisal.Guarantee(s.get(Member, g.guarantor_member_id).name, g.amount_cents,
                                guarantors.effective_status(g)) for g in rows]


def _application_out(s: Session, a: LoanApplication, live: bool = False) -> dict:
    m, prod = s.get(Member, a.member_id), s.get(LoanProduct, a.product_id)
    decisions = s.scalars(select(LoanDecision).where(LoanDecision.application_id == a.id).order_by(LoanDecision.id))
    out = {"id": a.id, "status": a.status, "member_no": m.member_no, "member": m.name,
           "product_id": prod.id, "product": prod.code, "product_name": prod.name,
           "amount_kes": a.amount_cents / 100, "term_months": a.term_months, "purpose": a.purpose,
           "approvals_needed": a.approvals_needed, "override_reason": a.override_reason,
           "prepared_by_user_id": a.prepared_by_user_id, "created_at": a.created_at,
           "submitted_at": a.submitted_at, "decided_at": a.decided_at, "exported_at": a.exported_at,
           "disbursed_at": a.disbursed_at, "appraisal": a.appraisal, "appraisal_outcome": a.appraisal_outcome,
           "decisions": [{"user_id": d.user_id, "decision": d.decision, "note": d.note,
                          "appraisal_outcome": d.appraisal_outcome, "at": d.at} for d in decisions]}
    if live and a.status in ("draft", "submitted"):
        out["appraisal"] = _appraisal_out(_appraise_application(s, a))
        out["appraisal_outcome"] = out["appraisal"]["outcome"]
    return out


@app.post("/institutions/{institution_id}/loan-applications", tags=["loans"])
def create_application(institution_id: int, body: ApplicationIn, s: Session = Depends(get_session),
                       who: Principal = Depends(require("loan_apply"))):
    prod = _get_product(s, institution_id, body.product_id)
    if not prod.active:
        raise HTTPException(409, f"product {prod.code} is not active")
    m, _, _ = _member_facts(s, institution_id, body.member_no)
    a = LoanApplication(institution_id=institution_id, member_id=m.id, product_id=prod.id,
                        amount_cents=_cents(body.amount_kes), term_months=body.term_months, purpose=body.purpose,
                        status="draft", prepared_by_user_id=who.id, created_at=auth.utcnow())
    s.add(a)
    s.flush()
    audit.record(s, who, "loan_application.create", "loan_application", a.id,
                 after={"member_no": m.member_no, "product": prod.code, "amount_cents": a.amount_cents,
                        "term_months": a.term_months})
    s.commit()
    return _application_out(s, a, live=True)


@app.get("/institutions/{institution_id}/loan-applications", dependencies=[Depends(require("read"))], tags=["loans"])
def list_applications(institution_id: int, status: str | None = None, limit: int = Query(100, ge=1, le=500),
                      s: Session = Depends(get_session)):
    q = select(LoanApplication).where(LoanApplication.institution_id == institution_id)
    if status:
        q = q.where(LoanApplication.status.in_(status.split(",")))
    return [_application_out(s, a) for a in s.scalars(q.order_by(LoanApplication.id.desc()).limit(limit))]


@app.get("/institutions/{institution_id}/loan-applications/{app_id}", dependencies=[Depends(require("read"))],
         tags=["loans"])
def get_application(institution_id: int, app_id: int, s: Session = Depends(get_session)):
    """Drafts and submitted applications are appraised afresh on every read; decided ones show their snapshot."""
    return _application_out(s, _get_application(s, institution_id, app_id), live=True)


@app.patch("/institutions/{institution_id}/loan-applications/{app_id}", tags=["loans"])
def update_application(institution_id: int, app_id: int, body: ApplicationPatch, s: Session = Depends(get_session),
                       who: Principal = Depends(require("loan_apply"))):
    a = _get_application(s, institution_id, app_id, lock=True)
    if a.status != "draft":
        raise HTTPException(409, "only a draft can be changed; withdraw and start again")
    before = {"amount_cents": a.amount_cents, "term_months": a.term_months, "purpose": a.purpose}
    if body.amount_kes is not None:
        a.amount_cents = _cents(body.amount_kes)
    if body.term_months is not None:
        a.term_months = body.term_months
    if body.purpose is not None:
        a.purpose = body.purpose
    after = {"amount_cents": a.amount_cents, "term_months": a.term_months, "purpose": a.purpose}
    audit.record(s, who, "loan_application.update", "loan_application", a.id,
                 before={k: v for k, v in before.items() if after[k] != v},
                 after={k: v for k, v in after.items() if before[k] != v})
    s.commit()
    return _application_out(s, a, live=True)


@app.post("/institutions/{institution_id}/loan-applications/{app_id}/submit", tags=["loans"])
def submit_application(institution_id: int, app_id: int, s: Session = Depends(get_session),
                       who: Principal = Depends(require("loan_apply"))):
    """Hand the application to the approvers, with the appraisal as it stands now."""
    a = _get_application(s, institution_id, app_id, lock=True)
    if a.status != "draft":
        raise HTTPException(409, f"application is already {a.status}")
    result = _appraise_application(s, a)
    prod = s.get(LoanProduct, a.product_id)
    a.appraisal, a.appraisal_outcome = _appraisal_out(result), result.outcome
    above = prod.second_approval_above_cents is not None and a.amount_cents > prod.second_approval_above_cents
    a.approvals_needed = 2 if above else 1
    a.status, a.submitted_at = "submitted", auth.utcnow()
    audit.record(s, who, "loan_application.submit", "loan_application", a.id,
                 after={"appraisal_outcome": result.outcome, "approvals_needed": a.approvals_needed})
    s.commit()
    return _application_out(s, a, live=True)


@app.post("/institutions/{institution_id}/loan-applications/{app_id}/withdraw", tags=["loans"])
def withdraw_application(institution_id: int, app_id: int, note: str = Query(..., min_length=3),
                         s: Session = Depends(get_session), who: Principal = Depends(require("loan_apply"))):
    a = _get_application(s, institution_id, app_id, lock=True)
    if a.status not in ("draft", "submitted"):
        raise HTTPException(409, f"an application that is {a.status} cannot be withdrawn")
    before = a.status
    a.status, a.decided_at = "withdrawn", auth.utcnow()
    _release_guarantees(s, a, who, "application withdrawn")
    audit.record(s, who, "loan_application.withdraw", "loan_application", a.id, before={"status": before},
                 after={"status": "withdrawn"}, note=note)
    s.commit()
    return _application_out(s, a)


@app.post("/institutions/{institution_id}/loan-applications/{app_id}/decide", tags=["loans"])
def decide_application(institution_id: int, app_id: int, body: DecisionIn, s: Session = Depends(get_session),
                       who: Principal = Depends(require("loan_approve"))):
    """An approver's decision. Never on an application they prepared; one decline ends it; approval needs the
    product's number of distinct approvers. Approving despite a failed or unknown check needs a written reason."""
    a = _get_application(s, institution_id, app_id, lock=True)  # row lock: two approvers at once stay consistent
    if a.status != "submitted":
        raise HTTPException(409, f"application is {a.status}, not waiting for a decision")
    if a.prepared_by_user_id == who.id:
        raise HTTPException(403, "you prepared this application, so another approver must decide")
    if s.scalar(select(LoanDecision.id).where(LoanDecision.application_id == a.id, LoanDecision.user_id == who.id)):
        raise HTTPException(409, "you have already decided on this application")
    result = _appraise_application(s, a)  # decide on today's figures, not the ones at submission
    if body.decision == "approve" and result.outcome != "passes" and not body.override_reason:
        raise HTTPException(422, f"the appraisal {result.outcome}: give an override_reason to approve anyway")
    now = auth.utcnow()
    s.add(LoanDecision(institution_id=institution_id, application_id=a.id, user_id=who.id, decision=body.decision,
                       note=body.note, appraisal_outcome=result.outcome, at=now))
    s.flush()
    a.appraisal, a.appraisal_outcome = _appraisal_out(result), result.outcome
    if body.decision == "approve" and result.outcome != "passes":
        a.override_reason = "\n".join(x for x in (a.override_reason, body.override_reason) if x)
    approvals = s.scalar(select(func.count()).where(LoanDecision.application_id == a.id,
                                                    LoanDecision.decision == "approve"))
    before = a.status
    if body.decision == "decline":
        a.status, a.decided_at = "declined", now
        _release_guarantees(s, a, who, "application declined")
    elif approvals >= a.approvals_needed:
        a.status, a.decided_at = "approved", now
    audit.record(s, who, f"loan_application.{body.decision}", "loan_application", a.id,
                 before={"status": before}, note=body.note,
                 after={"status": a.status, "appraisal_outcome": result.outcome,
                        "approvals": approvals, "approvals_needed": a.approvals_needed,
                        "override_reason": body.override_reason if body.decision == "approve" else None})
    s.commit()
    return _application_out(s, a)


@app.post("/institutions/{institution_id}/loan-applications/export.csv", tags=["loans"])
def export_approved(institution_id: int, s: Session = Depends(get_session),
                    who: Principal = Depends(require("loan_export"))):
    """Approved loans not yet handed over, as a file for the core system to disburse. Each loan is in exactly
    one export; it is marked exported here. Sawazi never disburses."""
    apps = list(s.scalars(select(LoanApplication).where(LoanApplication.institution_id == institution_id,
                                                        LoanApplication.status == "approved")
                          .order_by(LoanApplication.id).with_for_update()))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["sawazi_ref", "member_no", "member_name", "product_code", "amount", "term_months", "approved_at",
                "approved_by", "with_exceptions"])
    now = auth.utcnow()
    for a in apps:
        m, prod = s.get(Member, a.member_id), s.get(LoanProduct, a.product_id)
        approvers = [s.get(StaffUser, d.user_id).name for d in s.scalars(
            select(LoanDecision).where(LoanDecision.application_id == a.id, LoanDecision.decision == "approve"))]
        w.writerow([f"SWZ-{a.id}", m.member_no, m.name, prod.code, f"{a.amount_cents / 100:.2f}", a.term_months,
                    a.decided_at.strftime("%Y-%m-%d %H:%M"), "; ".join(approvers), "yes" if a.override_reason else "no"])
        a.status, a.exported_at = "exported", now
    audit.record(s, who, "loan_application.export", after={"applications": [a.id for a in apps]})
    s.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename=sawazi_loans_{now:%Y%m%d_%H%M}.csv"})


# ---------------------------------------------------------------- guarantors

class GuarantorIn(BaseModel):
    member_no: str
    amount_kes: Decimal = Field(gt=0, max_digits=14, decimal_places=2)


def _guarantee_out(s: Session, g: Guarantee) -> dict:
    m = s.get(Member, g.guarantor_member_id)
    return {"id": g.id, "application_id": g.application_id, "member_no": m.member_no, "name": m.name,
            "phone": g.phone, "amount_kes": g.amount_cents / 100, "status": guarantors.effective_status(g),
            "requested_at": g.requested_at, "expires_at": g.expires_at, "responded_at": g.responded_at}


def _release_guarantees(s: Session, a: LoanApplication, who: Principal, why: str) -> None:
    for g in s.scalars(select(Guarantee).where(Guarantee.application_id == a.id,
                                               Guarantee.status.in_(("requested", "accepted")))):
        before = g.status
        g.status = "released" if g.status == "accepted" else "cancelled"
        audit.record(s, who, "guarantee.release", "guarantee", g.id, before={"status": before},
                     after={"status": g.status}, note=why)


@app.get("/institutions/{institution_id}/loan-applications/{app_id}/guarantors",
         dependencies=[Depends(require("read"))], tags=["guarantors"])
def list_guarantors(institution_id: int, app_id: int, s: Session = Depends(get_session)):
    a = _get_application(s, institution_id, app_id)
    rows = s.scalars(select(Guarantee).where(Guarantee.application_id == a.id).order_by(Guarantee.id))
    return [_guarantee_out(s, g) for g in rows]


@app.post("/institutions/{institution_id}/loan-applications/{app_id}/guarantors", tags=["guarantors"])
def request_guarantor(institution_id: int, app_id: int, body: GuarantorIn, s: Session = Depends(get_session),
                      who: Principal = Depends(require("loan_apply")), provider=Depends(sms.get_provider)):
    """Ask a member to guarantee part of this loan. They get an SMS with a one-time link to accept or decline."""
    base = guarantors.public_url()
    if not base:
        raise HTTPException(503, "SAWAZI_PUBLIC_URL is not set (the https address members open links on)")
    a = _get_application(s, institution_id, app_id, lock=True)
    if a.status not in guarantors.OPEN_APPLICATION:
        raise HTTPException(409, f"application is {a.status}; guarantors can only be added before a decision")
    settings = _sms_settings(s, institution_id)
    if provider.name != "simulate" and not settings.enabled:
        raise HTTPException(409, "SMS is not switched on for this institution yet (admin: SMS settings)")
    g_member = s.scalar(select(Member).where(Member.institution_id == institution_id,
                                             Member.member_no == body.member_no))
    if not g_member:
        raise HTTPException(404, "guarantor is not a member of this institution")
    if g_member.id == a.member_id:
        raise HTTPException(422, "a member cannot guarantee their own loan")
    if s.scalar(select(Guarantee.id).where(Guarantee.application_id == a.id, Guarantee.guarantor_member_id == g_member.id,
                                           Guarantee.status.in_(("requested", "accepted")))):
        raise HTTPException(409, f"{g_member.member_no} has already been asked for this loan")
    phone = norm_phone(g_member.phone)
    if not phone:
        raise HTTPException(422, f"{g_member.member_no} has no valid phone number on record")
    if _opted_out(s, institution_id, phone):
        raise HTTPException(409, f"{g_member.member_no} has opted out of SMS; they must confirm in the branch")
    prod = s.get(LoanProduct, a.product_id)
    behind = s.scalar(select(func.max(Loan.days_in_arrears)).where(
        Loan.institution_id == institution_id, Loan.member_id == g_member.id, Loan.status == "active")) or 0
    if behind > prod.max_arrears_days:
        raise HTTPException(422, f"{g_member.member_no} has a loan {behind} days in arrears and cannot guarantee")
    amount = _cents(body.amount_kes)
    free = guarantors.free_capacity(s, g_member)
    if free is None:
        raise HTTPException(422, f"{g_member.member_no}'s deposits are not known: upload member balances first")
    if amount > free:
        raise HTTPException(422, f"{g_member.member_no} can guarantee at most {guarantors.kes(max(free, 0))} "
                                 f"(deposits less what they already guarantee)")

    token, now = guarantors.new_token(), auth.utcnow()
    g = Guarantee(institution_id=institution_id, application_id=a.id, guarantor_member_id=g_member.id,
                  amount_cents=amount, status="requested", phone=phone, token_hash=guarantors.token_hash(token),
                  expires_at=now + timedelta(days=guarantors.LINK_DAYS), requested_by_user_id=who.id,
                  requested_at=now)
    s.add(g)
    s.flush()
    inst, applicant = s.get(Institution, institution_id), s.get(Member, a.member_id)
    text = guarantors.request_sms(inst, applicant, a, g, f"{base}/g/{token}")
    msg = SmsMessage(institution_id=institution_id, member_id=g_member.id, phone=phone,
                     body=guarantors.redact(text, token), provider=provider.name, status="pending",
                     approved_by_user_id=who.id, approved_at=now)
    s.add(msg)
    s.flush()
    audit.record(s, who, "guarantee.request", "guarantee", g.id,
                 after={"application_id": a.id, "guarantor": g_member.member_no, "amount_cents": amount,
                        "phone": phone})
    s.commit()  # on record before the SMS leaves
    try:
        res = provider.send(phone, text, settings.service_name)
    except Exception as e:
        res = sms.SendResult("unknown", description=f"Error while sending ({type(e).__name__})")
    msg.status, msg.provider_message_id = res.status, res.provider_message_id
    msg.provider_status, msg.provider_description = res.provider_status, res.description
    msg.sent_at = auth.utcnow() if res.status != "failed" else None
    s.commit()
    return {**_guarantee_out(s, g), "sms": res.status}


@app.post("/institutions/{institution_id}/loan-applications/{app_id}/guarantors/{guarantee_id}/cancel",
          tags=["guarantors"])
def cancel_guarantor(institution_id: int, app_id: int, guarantee_id: int, note: str = Query(..., min_length=3),
                     s: Session = Depends(get_session), who: Principal = Depends(require("loan_apply"))):
    a = _get_application(s, institution_id, app_id, lock=True)
    g = s.get(Guarantee, guarantee_id)
    if not g or g.application_id != a.id:
        raise HTTPException(404, "guarantee not found")
    if a.status not in guarantors.OPEN_APPLICATION or g.status not in ("requested", "accepted"):
        raise HTTPException(409, "this guarantee can no longer be cancelled")
    before = g.status
    g.status = "released" if g.status == "accepted" else "cancelled"
    audit.record(s, who, "guarantee.cancel", "guarantee", g.id, before={"status": before},
                 after={"status": g.status}, note=note)
    s.commit()
    return _guarantee_out(s, g)


@app.get("/institutions/{institution_id}/members/{member_no}/guarantor-exposure",
         dependencies=[Depends(require("read"))], tags=["guarantors"])
def guarantor_exposure(institution_id: int, member_no: str, s: Session = Depends(get_session)):
    """What a member has pledged for other people's loans, and how much more they could guarantee."""
    m = s.scalar(select(Member).where(Member.institution_id == institution_id, Member.member_no == member_no))
    if not m:
        raise HTTPException(404, "member not found")
    rows = list(s.scalars(select(Guarantee).where(Guarantee.institution_id == institution_id,
                                                  Guarantee.guarantor_member_id == m.id,
                                                  Guarantee.status.in_(("requested", "accepted")))))
    free = guarantors.free_capacity(s, m)
    out = []
    for g in rows:
        if guarantors.effective_status(g) == "expired":
            continue
        row = _guarantee_out(s, g)
        a = s.get(LoanApplication, g.application_id)
        ln = s.get(Loan, a.disbursed_loan_id) if a.disbursed_loan_id else None
        if ln is not None and g.status == "accepted":
            # Their share of what is still owed: the pledge shrinks with the loan, for information.
            # The full pledge stays committed until the loan is repaid.
            share = -(-g.amount_cents * max(ln.balance_cents, 0) // max(ln.principal_cents, 1))
            row |= {"loan_no": ln.loan_no, "loan_balance_kes": ln.balance_cents / 100,
                    "at_risk_kes": min(share, g.amount_cents) / 100}
        out.append(row)
    for cg, ln, borrower in s.execute(
            select(CoreGuarantee, Loan, Member).join(Loan, CoreGuarantee.loan_id == Loan.id)
            .join(Member, Loan.member_id == Member.id)
            .where(CoreGuarantee.institution_id == institution_id, CoreGuarantee.guarantor_member_id == m.id,
                   CoreGuarantee.status == "active", guarantors.core_counts())):
        share = -(-cg.amount_cents * max(ln.balance_cents, 0) // max(ln.principal_cents, 1))
        out.append({"source": "core", "member_no": borrower.member_no, "name": borrower.name, "loan_no": ln.loan_no,
                    "amount_kes": cg.amount_cents / 100, "status": "accepted", "loan_balance_kes": ln.balance_cents / 100,
                    "at_risk_kes": min(share, cg.amount_cents) / 100})
    return {"member_no": m.member_no, "name": m.name,
            "deposits_kes": m.deposits_cents / 100 if m.deposits_cents is not None else None,
            "pledged_kes": guarantors.pledged_cents(s, institution_id, m.id) / 100,
            "free_kes": free / 100 if free is not None else None,
            "at_risk_kes": sum(r.get("at_risk_kes", 0) for r in out),
            "guarantees": out}


# ---------------------------------------------------------------- the guarantor's own page (public, by link)

def _by_token(s: Session, token: str) -> Guarantee | None:
    if len(token) > 40:
        return None
    return s.scalar(select(Guarantee).where(Guarantee.token_hash == guarantors.token_hash(token)))


def _consent_page(s: Session, g: Guarantee | None, token: str, notice: str = "", error: str = "") -> HTMLResponse:
    G = guarantors
    if g is None:
        return HTMLResponse(G.page("Link not valid", "<h1>This link is not valid</h1>",
                                   "<p>Check the SMS and try again, or contact your SACCO.</p>"), status_code=404)
    a, inst = s.get(LoanApplication, g.application_id), s.get(Institution, g.institution_id)
    applicant, prod = s.get(Member, a.member_id), s.get(LoanProduct, a.product_id)
    head = G.summary(inst, applicant, a, prod, g)
    status = G.effective_status(g)
    if status != "requested" or a.status not in G.OPEN_APPLICATION:
        done = {"accepted": "You accepted. Thank you.", "declined": "You declined. Nothing more is needed.",
                "expired": "This request has expired. Contact your SACCO if you still want to guarantee."}
        msg = done.get(status, "This request is no longer open.")
        return HTMLResponse(G.page("Guarantee", head, f'<div class="note info">{G.esc(msg)}</div>'))
    alerts = (f'<div class="note ok" role="status">{G.esc(notice)}</div>' if notice else "") + \
             (f'<div class="note err" role="alert">{G.esc(error)}</div>' if error else "")
    t = G.esc(token)
    forms = f"""
<form method="post" action="/g/{t}/pin" class="stack"><p class="small">To accept, get a PIN by SMS on
{G.esc(g.phone[:6] + '***' + g.phone[-3:])}.</p><button type="submit">Send me a PIN</button></form>
<form method="post" action="/g/{t}/accept" class="stack">
<label>PIN from the SMS<input name="pin" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{{6}}"
maxlength="6" required></label><button type="submit" class="primary">Accept: I guarantee {G.esc(G.kes(g.amount_cents))}</button></form>
<form method="post" action="/g/{t}/decline"><button type="submit" class="danger">Decline</button></form>"""
    return HTMLResponse(G.page("Guarantee", head, alerts, forms))


_CONSENT_NOTES = {
    "pin": ("PIN sent. It works for 10 minutes.", ""),
    "pin-limit": ("", "Too many PINs requested. Contact your SACCO."),
    "pin-failed": ("", "We could not send the PIN. Try again in a minute."),
    "pin-first": ("", "Ask for a PIN first (or a new one: it lasts 10 minutes)."),
    "pin-locked": ("", "Too many wrong PINs. Ask for a new PIN."),
    "pin-wrong": ("", "That PIN is not right."),
    "capacity": ("", "Your deposits no longer cover this amount. Contact your SACCO."),
}


def _back(token: str, code: str | None = None) -> RedirectResponse:
    """Post/redirect/get: refreshing the page never re-sends a form (and never sends another PIN)."""
    return RedirectResponse(f"/g/{token}" + (f"?m={code}" if code else ""), status_code=303)


@app.get("/g/{token}", include_in_schema=False)
def consent_page(token: str, m: str | None = None, s: Session = Depends(get_session)):
    notice, error = _CONSENT_NOTES.get(m or "", ("", ""))
    return _consent_page(s, _by_token(s, token), token, notice=notice, error=error)


@app.post("/g/{token}/pin", include_in_schema=False)
def consent_pin(token: str, request: Request, s: Session = Depends(get_session), provider=Depends(sms.get_provider)):
    g = _by_token(s, token)
    if g is None:
        return _consent_page(s, g, token)
    if guarantors.effective_status(g) != "requested":
        return _back(token)
    if g.pins_sent >= guarantors.MAX_PINS:
        return _back(token, "pin-limit")
    pin = f"{secrets.randbelow(10 ** 6):06d}"
    g.pin_hash, g.pin_expires_at = guarantors.pin_hash(g, pin), auth.utcnow() + timedelta(minutes=guarantors.PIN_MINUTES)
    g.pin_attempts, g.pins_sent = 0, g.pins_sent + 1
    s.commit()
    a, inst = s.get(LoanApplication, g.application_id), s.get(Institution, g.institution_id)
    settings = _sms_settings(s, g.institution_id)
    try:
        provider.send(g.phone, guarantors.pin_sms(inst, s.get(Member, a.member_id), pin), settings.service_name)
    except Exception:
        log.exception("guarantor PIN SMS failed")
        return _back(token, "pin-failed")
    return _back(token, "pin")


@app.post("/g/{token}/accept", include_in_schema=False)
def consent_accept(token: str, request: Request, pin: str = Form(""), s: Session = Depends(get_session)):
    pin = pin.strip()
    g = _by_token(s, token)
    if g is None:
        return _consent_page(s, g, token)
    if guarantors.effective_status(g) != "requested":
        return _back(token)
    g = s.scalar(select(Guarantee).where(Guarantee.id == g.id).with_for_update())
    a = s.get(LoanApplication, g.application_id)
    if a.status not in guarantors.OPEN_APPLICATION:
        return _back(token)
    if g.pin_hash is None or g.pin_expires_at <= auth.utcnow():
        return _back(token, "pin-first")
    if g.pin_attempts >= guarantors.MAX_PIN_ATTEMPTS:
        return _back(token, "pin-locked")
    if not hmac.compare_digest(guarantors.pin_hash(g, pin), g.pin_hash):
        g.pin_attempts += 1
        s.commit()
        return _back(token, "pin-wrong")
    result = guarantors.record_answer(s, g.id, "accept", request.client.host if request.client else None, "web")
    if result == "capacity":
        return _back(token, "capacity")
    s.commit()
    return _back(token)


@app.post("/g/{token}/decline", include_in_schema=False)
def consent_decline(token: str, request: Request, s: Session = Depends(get_session)):
    g = _by_token(s, token)
    if g is None:
        return _consent_page(s, g, token)
    if guarantors.effective_status(g) != "requested":
        return _back(token)
    guarantors.record_answer(s, g.id, "decline", request.client.host if request.client else None, "web")
    s.commit()
    return _back(token)


# ---------------------------------------------------------------- USSD guarantor consent
# Taifa Mobile USSD (ussdbeta.taifamobile.co.ke/documentation): GET query, or POST as JSON, form or multipart,
# with MSISDN, SESSION_ID, SERVICE_CODE, USSD_STRING; reply text/plain starting CON or END.
# Register https://<host>/callbacks/ussd/<SAWAZI_USSD_CALLBACK_TOKEN> as the service's callback URL.
# On a shared code the first part of USSD_STRING is the routing shortcut (e.g. "100" for *252*100#):
# set SAWAZI_USSD_SHORTCUT to it so it is not read as a menu choice. A dedicated code has no shortcut.

def ussd_input(raw: str, shortcut: str | None) -> str:
    """The guarantor's own choices from the raw input path, e.g. "100*1*4321" -> "1*4321" (shortcut 100)."""
    parts = [p.strip() for p in (raw or "").split("*") if p.strip() != ""]  # as Taifa's sample: ignore empties
    if shortcut and parts and parts[0] == shortcut:
        parts = parts[1:]
    return "*".join(parts)


@app.api_route("/callbacks/ussd/{token}", methods=["GET", "POST"], include_in_schema=False)
async def ussd_callback(token: str, request: Request, s: Session = Depends(get_session)):
    expected = os.getenv("SAWAZI_USSD_CALLBACK_TOKEN")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(404, "not found")
    ip = request.client.host if request.client else None
    allowed = {x.strip() for x in os.getenv("SAWAZI_USSD_ALLOWED_IPS", "").split(",") if x.strip()}
    if allowed and ip not in allowed:
        raise HTTPException(404, "not found")
    fields: dict = dict(request.query_params)
    if request.method == "POST":
        if "json" in request.headers.get("content-type", ""):
            body = await request.json()
            fields |= body if isinstance(body, dict) else {}
        else:
            fields |= {k: v for k, v in (await request.form()).items() if isinstance(v, str)}

    def field(*names: str) -> str:
        return next((str(fields[n]) for n in names if fields.get(n) not in (None, "")), "")

    session_id = field("SESSION_ID", "sessionId")[:100]
    phone = norm_phone(field("MSISDN", "phoneNumber"))
    raw = field("USSD_STRING", "text")[:200]
    text = ussd_input(raw, os.getenv("SAWAZI_USSD_SHORTCUT", "").strip() or None)

    # database work off the event loop
    reply = await run_in_threadpool(ussd.handle, s, session_id, phone, text, ip)
    return PlainTextResponse(reply, media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------- exposure and risk

def _pledges(s: Session, institution_id: int) -> list:
    """Every live pledge: accepted in Sawazi (on a loan, or on an application still being decided) and active in
    the core system (unless Sawazi recorded the same one)."""
    out = [exposure.Pledge(g.guarantor_member_id, a.member_id, a.disbursed_loan_id, g.amount_cents, "sawazi")
           for g, a in s.execute(select(Guarantee, LoanApplication)
                                 .join(LoanApplication, Guarantee.application_id == LoanApplication.id)
                                 .where(Guarantee.institution_id == institution_id, Guarantee.status == "accepted"))]
    out += [exposure.Pledge(cg.guarantor_member_id, ln.member_id, ln.id, cg.amount_cents, "core")
            for cg, ln in s.execute(select(CoreGuarantee, Loan).join(Loan, CoreGuarantee.loan_id == Loan.id)
                                    .where(CoreGuarantee.institution_id == institution_id,
                                           CoreGuarantee.status == "active", guarantors.core_counts()))]
    return out


def _risk_report(s: Session, institution_id: int) -> exposure.Report:
    members = [exposure.MemberRow(m.id, m.member_no, m.name, m.deposits_cents, m.employer)
               for m in s.scalars(select(Member).where(Member.institution_id == institution_id))]
    loans = [exposure.LoanRow(ln.id, ln.loan_no, ln.member_id, ln.product, ln.balance_cents, ln.arrears_cents,
                              ln.days_in_arrears)
             for ln in s.scalars(select(Loan).where(Loan.institution_id == institution_id, Loan.status == "active"))]
    return exposure.report(members, loans, _pledges(s, institution_id))


def _kes_fields(d):
    """cents -> KES and basis points -> percent, recursively, for the JSON the console reads."""
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            if k.endswith("_cents"):
                out[k[:-6] + "_kes"] = v / 100 if v is not None else None
            elif k.endswith("_bps"):
                out[k[:-4] + "_pct"] = v / 100
            else:
                out[k] = _kes_fields(v)
        return out
    if isinstance(d, list):
        return [_kes_fields(x) for x in d]
    return d


@app.get("/institutions/{institution_id}/risk", dependencies=[Depends(require("read"))], tags=["risk"])
def risk(institution_id: int, s: Session = Depends(get_session)):
    """Loan classification and provisioning, PAR by product and employer, concentration, the guarantor network,
    and flags for a person to look at."""
    r = _risk_report(s, institution_id)
    return _kes_fields({"portfolio": r.portfolio, "classification": r.classification,
                        "par_by_product": r.par_by_product, "par_by_employer": r.par_by_employer,
                        "concentration": r.concentration, "guarantors": r.guarantors,
                        "flags": [{"kind": f.kind, "severity": f.severity, "member_nos": f.member_nos,
                                   "amount_cents": f.amount_cents, "message": f.message} for f in r.flags]})


# ---------------------------------------------------------------- staff web console
# Plain HTML/CSS/JS served by the API: no build step, nothing extra to host. It calls the JSON API
# above with the staff member's bearer token; the server enforces every permission.

CONSOLE_DIR = Path(__file__).parent / "console"
CONSOLE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
               "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def console_security_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/g/"):
        response.headers["Content-Security-Policy"] = guarantors.PAGE_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/console"):
        response.headers["Content-Security-Policy"] = CONSOLE_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/console/")


app.mount("/console", StaticFiles(directory=CONSOLE_DIR, html=True), name="console")
