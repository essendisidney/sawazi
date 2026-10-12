"""Robustness: password guessing is stopped, failures are explained with a reference, the SMS bill is capped,
and every PostgreSQL transaction has time limits."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from sawazi import auth, member_api, sms
from sawazi.api import app
from sawazi.db import get_session
from sawazi.models import AuditEvent, Member, StaffUser
from tests.conftest import TEST_DB_URL, make_engine
from tests.test_auth import PW, env, login  # noqa: F401  (env is a fixture)
from tests.test_guarantors import Phone


def test_wrong_passwords_lock_the_account_and_an_admin_reset_unlocks_it(env):
    c, Session = env
    for _ in range(auth.LOGIN_ATTEMPTS):
        assert c.post("/auth/login", json={"email": "viewer@a.test", "password": "wrong-password-1"}).status_code == 401
    r = c.post("/auth/login", json={"email": "viewer@a.test", "password": PW})  # even the right one waits now
    assert r.status_code == 423 and "Try again in" in r.json()["detail"]
    with Session() as s:
        notes = s.scalars(select(AuditEvent.note).where(AuditEvent.action == "auth.login_failed")).all()
        assert notes.count("wrong password") == auth.LOGIN_ATTEMPTS and "account locked after repeated wrong passwords" in notes
    assert c.post("/auth/login", json={"email": "admin@a.test", "password": PW}).status_code == 200  # others unaffected

    with Session() as s:
        uid = s.scalar(select(StaffUser.id).where(StaffUser.email == "viewer@a.test"))
    r = c.patch(f"/institutions/1/users/{uid}", headers=login(c, "admin@a.test"), json={"password": "admin-chose-it-1"})
    assert r.status_code == 200
    assert c.post("/auth/login", json={"email": "viewer@a.test", "password": "admin-chose-it-1"}).status_code == 200


def test_a_lock_expires_and_a_success_resets_the_count(env):
    c, Session = env
    for _ in range(auth.LOGIN_ATTEMPTS):
        c.post("/auth/login", json={"email": "viewer@a.test", "password": "wrong-password-1"})
    with Session() as s:
        u = s.scalar(select(StaffUser).where(StaffUser.email == "viewer@a.test"))
        u.locked_until = auth.utcnow() - timedelta(seconds=1)
        s.commit()
    assert c.post("/auth/login", json={"email": "viewer@a.test", "password": PW}).status_code == 200
    for _ in range(auth.LOGIN_ATTEMPTS - 1):  # the count started again after the good login
        c.post("/auth/login", json={"email": "viewer@a.test", "password": "wrong-password-1"})
    assert c.post("/auth/login", json={"email": "viewer@a.test", "password": PW}).status_code == 200


def test_unknown_emails_are_never_locked_or_distinguished(env):
    c, _ = env
    for _ in range(auth.LOGIN_ATTEMPTS + 2):
        r = c.post("/auth/login", json={"email": "nobody@a.test", "password": "wrong-password-1"})
        assert r.status_code == 401 and r.json()["detail"] == "invalid email or password"


def test_every_response_has_a_reference_and_failures_quote_it():
    def broken():
        raise RuntimeError("boom")

    def busy():
        raise OperationalError("SELECT 1", {}, Exception("canceling statement due to lock timeout"))

    c = TestClient(app, raise_server_exceptions=False)
    try:
        assert re.fullmatch(r"[0-9a-f]{8}", c.get("/site/site.css").headers["x-sawazi-ref"])
        app.dependency_overrides[get_session] = broken
        r = c.get("/healthz")
        assert r.status_code == 500 and "Please quote reference" in r.json()["detail"] and "boom" not in r.text
        app.dependency_overrides[get_session] = busy
        r = c.get("/healthz")
        assert r.status_code == 503 and r.headers["retry-after"] == "30"
        assert "Nothing was changed" in r.json()["detail"] and r.headers["x-sawazi-ref"] in r.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_session, None)


def test_sign_in_sms_stop_at_the_daily_cap(env, monkeypatch):
    c, Session = env
    phone = Phone()
    app.dependency_overrides[sms.get_provider] = lambda: phone
    monkeypatch.setattr(member_api, "OTP_DAILY_CAP", 1)
    try:
        with Session() as s:
            s.add_all([Member(id=10, institution_id=1, member_no="M1", name="Achieng Owino", phone="254711000010"),
                       Member(id=11, institution_id=1, member_no="M2", name="Otieno Owino", phone="254711000011")])
            s.commit()
        first = c.post("/m/otp", json={"phone": "0711 000 010"})
        second = c.post("/m/otp", json={"phone": "0711 000 011"})
        assert first.json() == second.json()  # the same reply: the cap is not revealed
        assert len(phone.sent) == 1
    finally:
        app.dependency_overrides.pop(sms.get_provider, None)


@pytest.mark.skipif(not TEST_DB_URL, reason="needs PostgreSQL (SAWAZI_TEST_DB_URL)")
def test_every_postgres_transaction_has_time_limits():
    eng = make_engine()
    with eng.begin() as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar() == "50s"
        assert conn.execute(text("SHOW lock_timeout")).scalar() == "20s"
