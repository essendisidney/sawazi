from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sawazi import auth
from sawazi.db import Base, get_session
from sawazi.models import ExceptionItem, Institution, StaffSession, StaffUser

PK = {"X-API-Key": "platform-test-key"}
PW = "correct-horse-1"


@pytest.fixture()
def env(monkeypatch):
    """Two institutions, each with one user per role, and an open exception each."""
    monkeypatch.setenv("SAWAZI_API_KEY", "platform-test-key")
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)

    s = Session()
    h = auth.hash_password(PW)
    for iid, slug in [(1, "a"), (2, "b")]:
        s.add(Institution(id=iid, name=f"SACCO {slug.upper()}"))
        s.flush()
        for role in auth.ROLES:
            s.add(StaffUser(institution_id=iid, email=f"{role}@{slug}.test", name=f"{role} {slug}", role=role,
                            password_hash=h, is_active=True))
        s.add(ExceptionItem(id=iid, institution_id=iid, kind="double_payment", detail="check", amount_cents=100))
    s.commit()
    s.close()

    def override():
        sess = Session()
        try:
            yield sess
        finally:
            sess.close()

    from sawazi.api import app
    app.dependency_overrides[get_session] = override
    yield TestClient(app), Session
    app.dependency_overrides.clear()


def login(c, email, password=PW):
    r = c.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


# ---------------------------------------------------------------- passwords and sessions

def test_password_hashing():
    h = auth.hash_password("s3cret-password")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert auth.verify_password("s3cret-password", h)
    assert not auth.verify_password("wrong-password", h)
    assert not auth.verify_password("x", "garbage")
    assert auth.hash_password("same") != auth.hash_password("same")  # salted


def test_login_and_me(env):
    c, _ = env
    h = login(c, "Viewer@A.test")  # email is case-insensitive
    me = c.get("/auth/me", headers=h).json()
    assert me["role"] == "viewer" and me["institution_id"] == 1 and me["last_login_at"]
    assert "password_hash" not in me


@pytest.mark.parametrize("email,password", [("admin@a.test", "wrong-password"), ("nobody@a.test", PW)])
def test_login_failure_is_generic(env, email, password):
    c, _ = env
    r = c.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 401 and r.json()["detail"] == "invalid email or password"


def test_no_token_or_bad_token_rejected(env):
    c, _ = env
    assert c.get("/institutions/1/dashboard").status_code == 401
    assert c.get("/institutions/1/dashboard", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.get("/institutions/1/dashboard", headers=PK).status_code == 401  # platform key is not a staff login


def test_logout_and_expiry(env):
    c, Session = env
    h = login(c, "viewer@a.test")
    assert c.post("/auth/logout", headers=h).status_code == 200
    assert c.get("/auth/me", headers=h).status_code == 401

    h = login(c, "viewer@a.test")
    with Session() as s:
        s.execute(StaffSession.__table__.update().values(expires_at=auth.utcnow() - timedelta(minutes=1)))
        s.commit()
    assert c.get("/auth/me", headers=h).status_code == 401


def test_tokens_stored_hashed(env):
    c, Session = env
    token = login(c, "viewer@a.test")["Authorization"].split()[1]
    with Session() as s:
        assert s.scalar(select(StaffSession.token_hash)) != token


def test_deactivated_user_loses_access_immediately(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    viewer = login(c, "viewer@a.test")
    with Session() as s:
        uid = s.scalar(select(StaffUser.id).where(StaffUser.email == "viewer@a.test"))
    assert c.patch(f"/institutions/1/users/{uid}", json={"is_active": False}, headers=admin).status_code == 200
    assert c.get("/auth/me", headers=viewer).status_code == 401
    assert c.post("/auth/login", json={"email": "viewer@a.test", "password": PW}).status_code == 401


def test_change_password_revokes_other_sessions(env):
    c, _ = env
    first = login(c, "accountant@a.test")
    second = login(c, "accountant@a.test")
    r = c.post("/auth/password", headers=second, json={"current_password": PW, "new_password": "short"})
    assert r.status_code == 422
    r = c.post("/auth/password", headers=second, json={"current_password": "wrong-one!!", "new_password": "a-new-password"})
    assert r.status_code == 401
    r = c.post("/auth/password", headers=second, json={"current_password": PW, "new_password": "a-new-password"})
    assert r.status_code == 200
    assert c.get("/auth/me", headers=first).status_code == 401
    assert c.get("/auth/me", headers=second).status_code == 200
    login(c, "accountant@a.test", "a-new-password")


# ---------------------------------------------------------------- roles

ROLE_CASES = [
    # (method, path, roles allowed)
    ("get", "/institutions/1/dashboard", {"admin", "accountant", "credit_officer", "viewer"}),
    ("get", "/institutions/1/exceptions", {"admin", "accountant", "credit_officer", "viewer"}),
    ("get", "/institutions/1/reminders", {"admin", "accountant", "credit_officer", "viewer"}),
    ("post", "/institutions/1/collections/queue", {"admin", "accountant", "credit_officer"}),
    ("post", "/institutions/1/match", {"admin", "accountant"}),
    ("get", "/institutions/1/exports/postings.csv", {"admin", "accountant"}),
    ("get", "/institutions/1/users", {"admin"}),
]


@pytest.mark.parametrize("role", auth.ROLES)
def test_role_matrix(env, role):
    c, _ = env
    h = login(c, f"{role}@a.test")
    for method, path, allowed in ROLE_CASES:
        r = getattr(c, method)(path, headers=h)
        if role in allowed:
            assert r.status_code == 200, (role, path, r.text)
        else:
            assert r.status_code == 403, (role, path, r.status_code)
    r = c.post("/exceptions/1/resolve", headers=h, json={"note": "checked"})
    assert r.status_code == (200 if role in {"admin", "accountant"} else 403), (role, r.text)


def test_imports_need_reconcile_role(env):
    c, _ = env
    files = {"file": ("m.csv", b"member_no,name\n")}
    assert c.post("/institutions/1/import/members", files=files, headers=login(c, "credit_officer@a.test")).status_code == 403
    assert c.post("/institutions/1/import/members", files=files, headers=login(c, "accountant@a.test")).status_code != 403


# ---------------------------------------------------------------- institution scoping

def test_cannot_see_or_touch_another_institution(env):
    c, Session = env
    h = login(c, "admin@a.test")  # admin of SACCO A, strongest role
    for path in ["/institutions/2/dashboard", "/institutions/2/exceptions", "/institutions/2/reminders",
                 "/institutions/2/users", "/institutions/2/exports/postings.csv"]:
        assert c.get(path, headers=h).status_code == 404, path
    assert c.post("/institutions/2/match", headers=h).status_code == 404
    assert c.post("/institutions/999/match", headers=h).status_code == 404  # same answer as a real one
    # SACCO B's exception, addressed directly by id
    assert c.post("/exceptions/2/resolve", headers=h, json={"note": "x"}).status_code == 404
    with Session() as s:
        assert s.get(ExceptionItem, 2).status == "open"
        b_user = s.scalar(select(StaffUser.id).where(StaffUser.email == "viewer@b.test"))
    r = c.patch(f"/institutions/1/users/{b_user}", headers=h, json={"is_active": False})
    assert r.status_code == 404
    r = c.post("/institutions/2/users", headers=h,
               json={"email": "x@b.test", "name": "X", "role": "admin", "password": "long-enough-pw"})
    assert r.status_code == 404


def test_users_list_is_scoped(env):
    c, _ = env
    users = c.get("/institutions/1/users", headers=login(c, "admin@a.test")).json()
    assert len(users) == 4 and {u["institution_id"] for u in users} == {1}


# ---------------------------------------------------------------- user management

def test_admin_manages_staff(env):
    c, _ = env
    h = login(c, "admin@a.test")
    body = {"email": "new@a.test", "name": "New Officer", "role": "credit_officer", "password": "long-enough-pw"}
    r = c.post("/institutions/1/users", headers=h, json=body)
    assert r.status_code == 200 and r.json()["role"] == "credit_officer"
    assert c.post("/institutions/1/users", headers=h, json=body).status_code == 409  # duplicate email
    assert c.post("/institutions/1/users", headers=h, json={**body, "email": "z@a.test", "role": "superuser"}).status_code == 422
    assert c.post("/institutions/1/users", headers=h, json={**body, "email": "z@a.test", "password": "short"}).status_code == 422

    officer = login(c, "new@a.test", "long-enough-pw")
    r = c.patch(f"/institutions/1/users/{r.json()['id']}", headers=h, json={"role": "accountant"})
    assert r.status_code == 200 and r.json()["role"] == "accountant"
    assert c.get("/auth/me", headers=officer).status_code == 401  # role change forces a fresh login


def test_admin_cannot_lock_themselves_out(env):
    c, _ = env
    h = login(c, "admin@a.test")
    me = c.get("/auth/me", headers=h).json()
    assert c.patch(f"/institutions/1/users/{me['id']}", headers=h, json={"role": "viewer"}).status_code == 409
    assert c.patch(f"/institutions/1/users/{me['id']}", headers=h, json={"is_active": False}).status_code == 409
    assert c.patch(f"/institutions/1/users/{me['id']}", headers=h, json={"name": "Renamed"}).status_code == 200


# ---------------------------------------------------------------- platform operator

def test_platform_endpoints_need_platform_key(env, monkeypatch):
    c, _ = env
    assert c.post("/institutions", json={"name": "X"}).status_code == 401
    assert c.post("/institutions", json={"name": "X"}, headers={"X-API-Key": "wrong"}).status_code == 401
    # a staff admin token is not enough to create institutions
    assert c.post("/institutions", json={"name": "X"}, headers=login(c, "admin@a.test")).status_code == 401
    iid = c.post("/institutions", json={"name": "New SACCO"}, headers=PK).json()["id"]

    first = {"email": "boss@new.test", "name": "Boss", "role": "accountant", "password": "long-enough-pw"}
    assert c.post(f"/institutions/{iid}/admin", headers=PK, json=first).status_code == 422  # must be admin
    assert c.post(f"/institutions/{iid}/admin", json={**first, "role": "admin"}).status_code == 401
    r = c.post(f"/institutions/{iid}/admin", headers=PK, json={**first, "role": "admin"})
    assert r.status_code == 200 and r.json()["institution_id"] == iid

    monkeypatch.delenv("SAWAZI_API_KEY")
    assert c.post("/institutions", json={"name": "Y"}, headers=PK).status_code == 503  # never open by default
