"""Staff login, roles and institution scoping.

Every staff user belongs to exactly one institution. A user can only ever see or
change that institution's data; asking for another institution's data returns
404 so we never confirm it exists.

Tokens are random and opaque. Only their SHA-256 is stored, so logout and
deactivation take effect immediately and a leaked database holds no usable tokens.

Institutions can also create API keys for machine access. They use the same
`Authorization: Bearer` header, start with `swz_`, carry a non-admin role, and
can never do the human-only actions (clearing suspense, managing staff or keys).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .db import get_session
from .models import ApiKey, StaffSession, StaffUser

ROLES = ("admin", "accountant", "credit_officer", "approver", "viewer")
SESSION_HOURS = 12
MIN_PASSWORD_LEN = 10

# Which roles may do what. Keep this the single source of truth.
PERMISSIONS: dict[str, set[str]] = {
    "read": {"admin", "accountant", "credit_officer", "approver", "viewer"},  # dashboard, exceptions, reminders
    "collections": {"admin", "accountant", "credit_officer"},      # build the collections queue
    "reconcile": {"admin", "accountant"},                          # imports, matching, check-off
    "resolve": {"admin", "accountant"},                            # clear suspense, close flags
    "export": {"admin", "accountant"},                             # postings file (member-level money)
    "manage_users": {"admin"},                                     # staff and API keys
    "audit": {"admin"},                                            # read the audit log
    "send_sms": {"admin", "accountant", "credit_officer"},         # approve SMS to members, record opt-outs
    "sms_settings": {"admin"},                                     # SMS setup, opting a number back in
    "allocation_rules": {"admin"},                                 # how payments are split
    "loan_products": {"admin"},                                    # loan products and their appraisal rules
    "loan_apply": {"admin", "credit_officer", "approver"},         # capture, submit, withdraw applications
    "loan_approve": {"approver"},                                  # credit committee: approve or decline
    "loan_export": {"admin", "accountant"},                        # hand approved loans to the core system
}
# A person must do these, never a machine: they move money to a member, message members,
# or change who has access.
HUMAN_ONLY = {"resolve", "manage_users", "send_sms", "sms_settings", "allocation_rules", "loan_products",
              "loan_apply", "loan_approve"}
API_KEY_ROLES = ("accountant", "credit_officer", "viewer")
API_KEY_PREFIX = "swz_"
_LAST_USED_EVERY = timedelta(minutes=1)  # don't write to the database on every request

# scrypt cost: ~16 MB memory, well under a second on a small VPS.
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


# Verified against when the email is unknown, so a miss costs the same time as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def check_password_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(422, f"password must be at least {MIN_PASSWORD_LEN} characters")


# ---------------------------------------------------------------- sessions

def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(s: Session, email: str, password: str) -> StaffUser | None:
    user = s.scalar(select(StaffUser).where(StaffUser.email == email.strip().lower()))
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok or not user.is_active:
        return None
    return user


def issue_session(s: Session, user: StaffUser) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now + timedelta(hours=SESSION_HOURS)
    s.add(StaffSession(institution_id=user.institution_id, user_id=user.id, token_hash=token_hash(token),
                       created_at=now, expires_at=expires))
    user.last_login_at = now
    return token, expires


def revoke_sessions(s: Session, user_id: int, except_token: str | None = None) -> None:
    q = update(StaffSession).where(StaffSession.user_id == user_id, StaffSession.revoked_at.is_(None))
    if except_token:
        q = q.where(StaffSession.token_hash != token_hash(except_token))
    s.execute(q.values(revoked_at=utcnow()))


def _invalid_session() -> HTTPException:
    return HTTPException(401, "session expired, please log in again", headers={"WWW-Authenticate": "Bearer"})


def bearer_token(authorization: str | None = Header(default=None)) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "login required", headers={"WWW-Authenticate": "Bearer"})
    return token


def current_session(token: str = Depends(bearer_token), s: Session = Depends(get_session)) -> StaffSession:
    sess = s.scalar(select(StaffSession).where(StaffSession.token_hash == token_hash(token)))
    if not sess or sess.revoked_at or sess.expires_at <= utcnow():
        raise _invalid_session()
    return sess


def current_user(sess: StaffSession = Depends(current_session), s: Session = Depends(get_session)) -> StaffUser:
    """A logged-in person. API keys are refused here (their token is never a session)."""
    user = s.get(StaffUser, sess.user_id)
    if not user or not user.is_active or user.institution_id != sess.institution_id:
        raise _invalid_session()
    return user


# ---------------------------------------------------------------- API keys

def new_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def _api_key(s: Session, token: str) -> ApiKey:
    key = s.scalar(select(ApiKey).where(ApiKey.key_hash == token_hash(token)))
    if not key or key.revoked_at:
        raise HTTPException(401, "invalid or revoked API key", headers={"WWW-Authenticate": "Bearer"})
    now = utcnow()
    if not key.last_used_at or now - key.last_used_at >= _LAST_USED_EVERY:
        key.last_used_at = now
        s.commit()
    return key


@dataclass(frozen=True)
class Principal:
    """Whoever is calling: a staff user or an institution API key."""

    kind: str  # user | api_key | platform | anonymous | provider | member
    id: int | None
    institution_id: int
    role: str
    name: str

    @property
    def is_user(self) -> bool:
        return self.kind == "user"

    @classmethod
    def of(cls, user: StaffUser) -> Principal:
        return cls("user", user.id, user.institution_id, user.role, user.name)


def current_principal(token: str = Depends(bearer_token), s: Session = Depends(get_session)) -> Principal:
    if token.startswith(API_KEY_PREFIX):
        k = _api_key(s, token)
        return Principal("api_key", k.id, k.institution_id, k.role, k.name)
    return Principal.of(current_user(current_session(token, s), s))


def require(action: str):
    """Dependency: the caller (staff user or API key) has a role allowed to do `action`, and —
    when the route has an {institution_id} — belongs to that institution."""
    allowed = PERMISSIONS[action]

    def dep(institution_id: int | None = None, who: Principal = Depends(current_principal)) -> Principal:
        if institution_id is not None:
            ensure_same_institution(who, institution_id)
        if not who.is_user and action in HUMAN_ONLY:
            raise HTTPException(403, "API keys cannot do this; a staff member must")
        if who.role not in allowed:
            raise HTTPException(403, "your role does not allow this")
        return who

    return dep


def ensure_same_institution(who: Principal, institution_id: int, what: str = "institution") -> None:
    if who.institution_id != institution_id:
        raise HTTPException(404, f"{what} not found")


# ---------------------------------------------------------------- platform operator

def platform_key(x_api_key: str | None = Header(default=None)) -> None:
    """Pesara-side key for creating institutions and their first admin. Never given to institutions."""
    expected = os.getenv("SAWAZI_API_KEY")
    if not expected:
        raise HTTPException(503, "platform key not configured (set SAWAZI_API_KEY)")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(401, "invalid platform key")
