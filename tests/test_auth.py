from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from sawazi import auth
from sawazi.db import get_session
from sawazi.models import ApiKey, ExceptionItem, Institution, StaffSession, StaffUser
from tests.conftest import make_engine

PK = {"X-API-Key": "platform-test-key"}
PW = "correct-horse-1"


@pytest.fixture()
def env(monkeypatch):
    """Two institutions, each with one user per role, and an open exception each."""
    monkeypatch.setenv("SAWAZI_API_KEY", "platform-test-key")
    eng = make_engine()
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
    ("get", "/institutions/1/dashboard", {"admin", "accountant", "credit_officer", "approver", "viewer"}),
    ("get", "/institutions/1/exceptions", {"admin", "accountant", "credit_officer", "approver", "viewer"}),
    ("get", "/institutions/1/reminders", {"admin", "accountant", "credit_officer", "approver", "viewer"}),
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
    assert len(users) == len(auth.ROLES) and {u["institution_id"] for u in users} == {1}


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


# ---------------------------------------------------------------- institution API keys

def make_key(c, admin, role="accountant", name="core banking sync", iid=1):
    r = c.post(f"/institutions/{iid}/api-keys", headers=admin, json={"name": name, "role": role})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_key_created_once_and_stored_hashed(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    k = make_key(c, admin)
    assert k["key"].startswith("swz_") and k["prefix"] == k["key"][:12]
    listed = c.get("/institutions/1/api-keys", headers=admin).json()
    assert len(listed) == 1 and "key" not in listed[0] and listed[0]["last_used_at"] is None
    with Session() as s:
        stored = s.scalar(select(ApiKey))
        assert stored.key_hash != k["key"] and k["key"] not in stored.key_hash
        assert stored.created_by_user_id == c.get("/auth/me", headers=admin).json()["id"]


@pytest.mark.parametrize("role", ["accountant", "credit_officer", "viewer"])
def test_api_key_follows_its_role(env, role):
    c, _ = env
    key = {"Authorization": f"Bearer {make_key(c, login(c, 'admin@a.test'), role=role)['key']}"}
    for method, path, allowed in ROLE_CASES:
        r = getattr(c, method)(path, headers=key)
        assert r.status_code == (200 if role in allowed else 403), (role, path, r.status_code)


def test_api_key_cannot_do_human_only_actions(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    key = {"Authorization": f"Bearer {make_key(c, admin, role='accountant')['key']}"}
    r = c.post("/exceptions/1/resolve", headers=key, json={"note": "x"})
    assert r.status_code == 403 and "staff member" in r.json()["detail"]
    with Session() as s:
        assert s.get(ExceptionItem, 1).status == "open"
    assert c.post("/institutions/1/api-keys", headers=key, json={"name": "n", "role": "viewer"}).status_code == 403
    assert c.get("/institutions/1/users", headers=key).status_code == 403
    # staff-only endpoints do not accept a key as a login
    assert c.get("/auth/me", headers=key).status_code == 401
    assert c.post("/auth/logout", headers=key).status_code == 401


def test_api_key_cannot_be_admin(env):
    c, _ = env
    admin = login(c, "admin@a.test")
    assert c.post("/institutions/1/api-keys", headers=admin, json={"name": "n", "role": "admin"}).status_code == 422


def test_only_admins_manage_api_keys(env):
    c, _ = env
    acc = login(c, "accountant@a.test")
    assert c.post("/institutions/1/api-keys", headers=acc, json={"name": "n", "role": "viewer"}).status_code == 403
    assert c.get("/institutions/1/api-keys", headers=acc).status_code == 403


def test_api_key_scoped_to_its_institution(env):
    c, _ = env
    a_key = {"Authorization": f"Bearer {make_key(c, login(c, 'admin@a.test'))['key']}"}
    b_key = make_key(c, login(c, "admin@b.test"), iid=2)
    assert c.get("/institutions/2/dashboard", headers=a_key).status_code == 404
    assert c.post("/institutions/2/match", headers=a_key).status_code == 404
    # A's admin can neither see nor revoke B's key
    admin_a = login(c, "admin@a.test")
    assert c.get("/institutions/2/api-keys", headers=admin_a).status_code == 404
    assert c.delete(f"/institutions/1/api-keys/{b_key['id']}", headers=admin_a).status_code == 404
    assert c.delete(f"/institutions/2/api-keys/{b_key['id']}", headers=admin_a).status_code == 404
    b_auth = {"Authorization": f"Bearer {b_key['key']}"}
    assert c.get("/institutions/2/dashboard", headers=b_auth).status_code == 200


def test_revoked_or_unknown_key_rejected(env):
    c, _ = env
    admin = login(c, "admin@a.test")
    k = make_key(c, admin)
    key = {"Authorization": f"Bearer {k['key']}"}
    assert c.get("/institutions/1/dashboard", headers=key).status_code == 200
    r = c.delete(f"/institutions/1/api-keys/{k['id']}", headers=admin)
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert c.get("/institutions/1/dashboard", headers=key).status_code == 401
    assert c.get("/institutions/1/dashboard", headers={"Authorization": "Bearer swz_madeup"}).status_code == 401
    assert len(c.get("/institutions/1/api-keys", headers=admin).json()) == 1  # kept for traceability


def test_api_key_survives_creator_deactivation_and_records_use(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    with Session() as s:
        second = StaffUser(institution_id=1, email="admin2@a.test", name="Admin 2", role="admin",
                           password_hash=auth.hash_password(PW), is_active=True)
        s.add(second)
        s.commit()
        second_id = second.id
    k = make_key(c, login(c, "admin2@a.test"))
    assert c.patch(f"/institutions/1/users/{second_id}", headers=admin, json={"is_active": False}).status_code == 200
    key = {"Authorization": f"Bearer {k['key']}"}
    assert c.get("/institutions/1/dashboard", headers=key).status_code == 200  # belongs to the institution
    assert c.get("/institutions/1/api-keys", headers=admin).json()[0]["last_used_at"]
