"""Sawazi by Pesara — HTTP API.

Run:  uvicorn sawazi.api:app --reload
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import audit, auth
from .auth import API_KEY_ROLES, ROLES, Principal, require
from .db import get_session, init_db
from .engine.checkoff import reconcile_checkoff
from .engine.collections import build_queue, portfolio_at_risk
from .engine.matching import allocate, run_matching
from .importers import sources
from .models import (Allocation, ApiKey, AuditEvent, ExceptionItem, Institution, Loan, Member, Reminder, StaffSession,
                     StaffUser, Transaction)

@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Sawazi by Pesara", version="0.1.0", lifespan=lifespan,
              description="Repayment matching, check-off reconciliation and collections for SACCOs and microfinance institutions.")

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
def me(user: StaffUser = Depends(auth.current_user)):
    return _user_out(user)


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
    return _audited(s, who, "match.run", run_matching(s, institution_id))


@app.post("/institutions/{institution_id}/collections/queue")
def collections_queue(institution_id: int, s: Session = Depends(get_session),
                      who: Principal = Depends(require("collections"))):
    _inst(s, institution_id)
    return _audited(s, who, "collections.queue", build_queue(s, institution_id))


@app.get("/institutions/{institution_id}/reminders", dependencies=[Depends(require("read"))])
def reminders(institution_id: int, limit: int = 50, s: Session = Depends(get_session)):
    rows = s.execute(
        select(Reminder, Loan, Member)
        .join(Loan, Reminder.loan_id == Loan.id)
        .join(Member, Loan.member_id == Member.id)
        .where(Reminder.institution_id == institution_id, Reminder.status == "queued")
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
    return [
        {"id": e.id, "kind": e.kind, "severity": e.severity, "amount_kes": e.amount_cents / 100,
         "detail": e.detail, "transaction_id": e.transaction_id, "member_id": e.member_id}
        for e in items
    ]


class ResolveIn(BaseModel):
    member_no: str | None = None  # for suspense: which member the money belongs to
    note: str | None = None


@app.post("/exceptions/{exception_id}/resolve")
def resolve(exception_id: int, body: ResolveIn, s: Session = Depends(get_session),
            user: Principal = Depends(require("resolve"))):
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
        tx[status]["kes"] += (total or 0) / 100
        tx[f"{source}_{status}"] = {"count": n, "kes": (total or 0) / 100}
    exc = defaultdict(lambda: {"count": 0, "kes": 0.0})
    for kind, n, total in s.execute(
        select(ExceptionItem.kind, func.count(), func.sum(ExceptionItem.amount_cents))
        .where(ExceptionItem.institution_id == institution_id, ExceptionItem.status == "open")
        .group_by(ExceptionItem.kind)
    ):
        exc[kind] = {"count": n, "kes": (total or 0) / 100}
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
    actor_kind: str | None = Query(None, pattern="^(user|api_key|platform|anonymous)$"),
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
