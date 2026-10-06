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
import threading
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import audit, auth, daraja, sms
from .auth import API_KEY_ROLES, PERMISSIONS, ROLES, Principal, require
from .db import get_session, init_db
from .engine.checkoff import reconcile_checkoff
from .engine.collections import build_queue, portfolio_at_risk
from .engine import allocation
from .engine.matching import allocate, run_matching
from .importers import sources
from .importers.common import norm_phone
from .models import (Allocation, AllocationRules, ApiKey, AuditEvent, ExceptionItem, Institution, Loan, Member, MpesaCallback,
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
    s: Session = Depends(get_session),
    who: Principal = Depends(require("reconcile")),
):
    _inst(s, institution_id)
    content = await file.read()
    note = f"file {file.filename}"
    if kind in IMPORTERS:
        return _audited(s, who, f"import.{kind}", IMPORTERS[kind](s, institution_id, content).as_dict(), note)
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
    actor_kind: str | None = Query(None, pattern="^(user|api_key|platform|anonymous|provider)$"),
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
