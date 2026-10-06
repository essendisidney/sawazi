"""Sawazi by Pesara — HTTP API.

Run:  uvicorn sawazi.api:app --reload
Docs: http://localhost:8000/docs
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth
from .auth import ROLES, require
from .db import get_session, init_db
from .engine.checkoff import reconcile_checkoff
from .engine.collections import build_queue, portfolio_at_risk
from .engine.matching import allocate, run_matching
from .importers import sources
from .models import (Allocation, ExceptionItem, Institution, Loan, Member, Reminder, StaffSession, StaffUser,
                     Transaction)

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


def _create_user(s: Session, institution_id: int, body: StaffIn) -> StaffUser:
    auth.check_password_policy(body.password)
    email = body.email.strip().lower()
    if s.scalar(select(StaffUser.id).where(StaffUser.email == email)):
        raise HTTPException(409, "a user with this email already exists")
    u = StaffUser(institution_id=institution_id, email=email, name=body.name, role=body.role,
                  password_hash=auth.hash_password(body.password), is_active=True)
    s.add(u)
    s.commit()
    return u


@app.post("/institutions/{institution_id}/admin", dependencies=[Depends(auth.platform_key)], tags=["platform"])
def create_first_admin(institution_id: int, body: StaffIn, s: Session = Depends(get_session)):
    """Create an institution's first admin. After this, that admin manages their own staff."""
    _inst(s, institution_id)
    if body.role != "admin":
        raise HTTPException(422, "the first user must be an admin")
    return _user_out(_create_user(s, institution_id, body))


@app.post("/auth/login", tags=["auth"])
def login(body: LoginIn, s: Session = Depends(get_session)):
    user = auth.authenticate(s, body.email, body.password)
    if not user:
        raise HTTPException(401, "invalid email or password")
    token, expires = auth.issue_session(s, user)
    s.commit()
    return {"token": token, "token_type": "bearer", "expires_at": expires, "user": _user_out(user)}


@app.post("/auth/logout", tags=["auth"])
def logout(sess: StaffSession = Depends(auth.current_session), s: Session = Depends(get_session)):
    sess.revoked_at = auth.utcnow()
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
    s.commit()
    return {"changed": True}


@app.get("/institutions/{institution_id}/users", dependencies=[Depends(require("manage_users"))], tags=["users"])
def list_users(institution_id: int, s: Session = Depends(get_session)):
    users = s.scalars(select(StaffUser).where(StaffUser.institution_id == institution_id).order_by(StaffUser.name))
    return [_user_out(u) for u in users]


@app.post("/institutions/{institution_id}/users", dependencies=[Depends(require("manage_users"))], tags=["users"])
def create_user(institution_id: int, body: StaffIn, s: Session = Depends(get_session)):
    return _user_out(_create_user(s, institution_id, body))


@app.patch("/institutions/{institution_id}/users/{user_id}", tags=["users"])
def update_user(institution_id: int, user_id: int, body: StaffPatch, s: Session = Depends(get_session),
                admin: StaffUser = Depends(require("manage_users"))):
    u = s.get(StaffUser, user_id)
    if not u or u.institution_id != institution_id:
        raise HTTPException(404, "user not found")
    if u.id == admin.id and ((body.role and body.role != "admin") or body.is_active is False):
        raise HTTPException(409, "you cannot remove your own admin access; ask another admin")
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
    s.commit()
    return _user_out(u)


IMPORTERS = {
    "members": sources.import_members,
    "loans": sources.import_loans,
    "mpesa": sources.import_mpesa_statement,
    "bank": sources.import_bank_statement,
}


@app.post("/institutions/{institution_id}/import/{kind}", dependencies=[Depends(require("reconcile"))])
async def import_file(
    institution_id: int,
    kind: str,
    file: UploadFile = File(...),
    employer: str | None = Query(None, description="check-off imports only"),
    period: str | None = Query(None, pattern=r"^\d{4}-\d{2}$", description="YYYY-MM, check-off imports only"),
    s: Session = Depends(get_session),
):
    _inst(s, institution_id)
    content = await file.read()
    if kind in IMPORTERS:
        return IMPORTERS[kind](s, institution_id, content).as_dict()
    if kind in {"checkoff_schedule", "checkoff_remittance"}:
        if not employer or not period:
            raise HTTPException(422, "employer and period are required for check-off imports")
        fn = sources.import_checkoff_schedule if kind == "checkoff_schedule" else sources.import_checkoff_remittance
        return fn(s, institution_id, employer, period, content).as_dict()
    raise HTTPException(404, f"unknown import kind '{kind}'")


@app.post("/institutions/{institution_id}/checkoff/reconcile", dependencies=[Depends(require("reconcile"))])
def checkoff(institution_id: int, employer: str, period: str, s: Session = Depends(get_session)):
    _inst(s, institution_id)
    return reconcile_checkoff(s, institution_id, employer, period)


@app.post("/institutions/{institution_id}/match", dependencies=[Depends(require("reconcile"))])
def match(institution_id: int, s: Session = Depends(get_session)):
    _inst(s, institution_id)
    return run_matching(s, institution_id)


@app.post("/institutions/{institution_id}/collections/queue", dependencies=[Depends(require("collections"))])
def collections_queue(institution_id: int, s: Session = Depends(get_session)):
    _inst(s, institution_id)
    return build_queue(s, institution_id)


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
            user: StaffUser = Depends(require("resolve"))):
    e = s.get(ExceptionItem, exception_id)
    if not e or e.status != "open" or e.institution_id != user.institution_id:
        raise HTTPException(404, "open exception not found")
    if e.kind == "suspense":
        if not body.member_no:
            raise HTTPException(422, "member_no is required to clear a suspense item")
        m = s.scalar(select(Member).where(Member.institution_id == e.institution_id, Member.member_no == body.member_no))
        if not m:
            raise HTTPException(404, "member not found")
        t = s.get(Transaction, e.transaction_id)
        t.match_method, t.match_confidence = "manual", 100
        allocs = allocate(s, t, m.id)
        e.detail += f" | Cleared to {m.member_no}" + (f": {body.note}" if body.note else "")
        e.status = "resolved"
        s.commit()
        return {"resolved": True, "allocations": [{"target": a.target, "amount_kes": a.amount_cents / 100} for a in allocs]}
    e.status = "resolved"
    if body.note:
        e.detail += f" | {body.note}"
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


@app.get("/institutions/{institution_id}/exports/postings.csv", dependencies=[Depends(require("export"))])
def postings(institution_id: int, s: Session = Depends(get_session)):
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
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=sawazi_postings.csv"})
