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
from sawazi.models import AuditEvent, Member, StaffUser, Transaction
from tests.conftest import TEST_DB_URL, make_engine
from tests.test_auth import PW, env, login  # noqa: F401  (env is a fixture)
from tests.test_guarantors import Phone
from tests.test_member_app import members  # noqa: F401  (a fixture)


def _try(c, email, password, ip=None):
    return c.post("/auth/login", json={"email": email, "password": password}, headers={"x-real-ip": ip} if ip else {})


def test_guessing_from_one_address_is_held_back_without_locking_out_the_person(env, monkeypatch):
    c, Session = env
    monkeypatch.setenv("VERCEL", "1")  # the caller's address comes from x-real-ip, as on the live site
    for _ in range(auth.LOGIN_ATTEMPTS):
        assert _try(c, "viewer@a.test", "wrong-password-1", "41.90.1.1").status_code == 401
    held = _try(c, "viewer@a.test", PW, "41.90.1.1")  # even the right password waits now, from that address
    assert held.status_code == 401 and held.json()["detail"] == "invalid email or password"  # reveals nothing
    assert _try(c, "viewer@a.test", PW, "41.90.2.2").status_code == 200  # the real person, elsewhere, gets in
    with Session() as s:
        notes = s.scalars(select(AuditEvent.note).where(AuditEvent.action == "auth.login_failed")).all()
        assert notes.count("wrong password") == auth.LOGIN_ATTEMPTS and "too many wrong tries from this address" in notes


def test_one_address_trying_many_accounts_is_held_back(env, monkeypatch):
    c, _ = env
    monkeypatch.setenv("VERCEL", "1")
    for i in range(auth.IP_ATTEMPTS):
        _try(c, f"guess{i}@a.test", "wrong-password-1", "41.90.3.3")
    assert _try(c, "admin@a.test", PW, "41.90.3.3").status_code == 401
    assert _try(c, "admin@a.test", PW, "41.90.4.4").status_code == 200


def test_a_good_login_or_an_admin_reset_clears_the_misses(env, monkeypatch):
    c, Session = env
    monkeypatch.setenv("VERCEL", "1")
    for _ in range(auth.LOGIN_ATTEMPTS - 1):
        _try(c, "viewer@a.test", "wrong-password-1", "41.90.5.5")
    assert _try(c, "viewer@a.test", PW, "41.90.5.5").status_code == 200
    for _ in range(auth.LOGIN_ATTEMPTS - 1):  # the count started again after the good login
        _try(c, "viewer@a.test", "wrong-password-1", "41.90.5.5")
    assert _try(c, "viewer@a.test", PW, "41.90.5.5").status_code == 200
    for _ in range(auth.LOGIN_ATTEMPTS):
        _try(c, "viewer@a.test", "wrong-password-1", "41.90.5.5")
    with Session() as s:
        uid = s.scalar(select(StaffUser.id).where(StaffUser.email == "viewer@a.test"))
    admin = login(c, "admin@a.test")
    assert c.patch(f"/institutions/1/users/{uid}", headers=admin, json={"password": "admin-chose-it-1"}).status_code == 200
    assert _try(c, "viewer@a.test", "admin-chose-it-1", "41.90.5.5").status_code == 200


def test_unknown_emails_get_the_same_answer(env):
    c, _ = env
    for _ in range(auth.LOGIN_ATTEMPTS + 2):
        r = _try(c, "nobody@a.test", "wrong-password-1")
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


def test_sign_in_sms_limits_are_per_sacco_and_per_address(env, monkeypatch):
    c, Session = env
    phone = Phone()
    app.dependency_overrides[sms.get_provider] = lambda: phone
    monkeypatch.setenv("VERCEL", "1")
    try:
        with Session() as s:
            s.add_all([Member(id=10, institution_id=1, member_no="M1", name="Achieng Owino", phone="254711000010"),
                       Member(id=11, institution_id=1, member_no="M2", name="Otieno Owino", phone="254711000011"),
                       Member(id=20, institution_id=2, member_no="B1", name="Wanjiru Kamau", phone="254711000020")])
            s.commit()
        monkeypatch.setattr(member_api, "OTP_DAILY_CAP", 1)
        first = c.post("/m/otp", json={"phone": "0711 000 010"}, headers={"x-real-ip": "41.90.6.1"})
        capped = c.post("/m/otp", json={"phone": "0711 000 011"}, headers={"x-real-ip": "41.90.6.2"})
        other_sacco = c.post("/m/otp", json={"phone": "0711 000 020"}, headers={"x-real-ip": "41.90.6.3"})
        assert first.json() == capped.json() == other_sacco.json()  # the same reply: limits are never revealed
        assert [m[0] for m in phone.sent] == ["254711000010", "254711000020"]  # SACCO A capped, SACCO B not
        monkeypatch.setattr(member_api, "OTP_DAILY_CAP", 500)
        monkeypatch.setattr(member_api, "OTP_PER_IP_HOUR", 1)
        c.post("/m/otp", json={"phone": "0711 000 011"}, headers={"x-real-ip": "41.90.6.1"})  # that address used its one
        assert len(phone.sent) == 2
        c.post("/m/otp", json={"phone": "0711 000 011"}, headers={"x-real-ip": "41.90.6.9"})
        assert len(phone.sent) == 3
    finally:
        app.dependency_overrides.pop(sms.get_provider, None)


def test_a_stolen_phone_is_signed_out(members):
    from tests.test_member_app import PIN, bearer, sign_up

    c, Session, phone = members
    stolen = sign_up(c, phone)
    for _ in range(member_api.PIN_ATTEMPTS * member_api.PIN_LOCKOUTS_BEFORE_SIGN_OUT):
        with Session() as s:  # skip the 15-minute waits between lock-outs
            from sawazi.models import MemberCredential
            cred = s.scalar(select(MemberCredential).where(MemberCredential.member_id == 10))
            cred.locked_until = None
            s.commit()
        r = c.post("/m/login", json={"device_token": stolen["device_token"], "pin": "9999"})
    assert r.json()["detail"] == "This phone is not set up. Sign in with an SMS code."
    assert c.post("/m/login", json={"device_token": stolen["device_token"], "pin": PIN}).status_code == 401
    old = sign_up(c, phone)
    new = sign_up(c, phone)  # the member sets up a new phone: the other one is signed out
    assert c.get("/m/me", headers=bearer(old["session_token"])).status_code == 401
    assert c.post("/m/login", json={"device_token": old["device_token"], "pin": PIN}).status_code == 401
    assert c.get("/m/me", headers=bearer(new["session_token"])).status_code == 200


@pytest.mark.skipif(not TEST_DB_URL, reason="needs PostgreSQL (SAWAZI_TEST_DB_URL)")
def test_every_postgres_transaction_has_time_limits():
    eng = make_engine()
    with eng.begin() as conn:
        assert conn.execute(text("SHOW statement_timeout")).scalar() == "50s"
        assert conn.execute(text("SHOW lock_timeout")).scalar() == "20s"


# ---------------------------------------------------------------- callbacks, postings, maker-checker, CSV

from tests.test_c2b import TOKEN as C2B_TOKEN, confirm, paybill, payload  # noqa: E402,F401  (paybill is a fixture)
from tests.test_loan_applications import apply, decide, submit, world  # noqa: E402,F401  (world is a fixture)


def test_callbacks_are_refused_without_an_allow_list_in_production(paybill, monkeypatch):
    c, Session = paybill
    monkeypatch.delenv("SAWAZI_CALLBACKS_OPEN")  # production: no sandbox opt-out
    assert confirm(c).status_code == 404
    monkeypatch.setenv("SAWAZI_SMS_CALLBACK_TOKEN", "sms-token-1234567890")
    assert c.post("/callbacks/taifa/sms-token-1234567890/incoming", json={"message": "STOP"}).status_code == 404
    monkeypatch.setenv("SAWAZI_DARAJA_ALLOWED_IPS", "testclient")  # the provider's address is listed
    assert confirm(c).status_code == 200
    with Session() as s:
        assert s.scalar(select(Transaction.reference)) == "TJ12ABC34D"


def test_real_time_payments_wait_for_the_statement_before_the_core_posts_them(paybill):
    from sawazi.models import MpesaCallback

    c, Session = paybill
    assert confirm(c).status_code == 200  # allocated in real time
    h = login(c, "accountant@a.test")
    r = c.get("/institutions/1/exports/postings.csv", headers=h)
    assert r.headers["x-sawazi-held-until-statement"] == "1" and "TJ12ABC34D" not in r.text
    with Session() as s:  # the paybill statement confirms it
        s.scalar(select(MpesaCallback)).statement_confirmed_at = auth.utcnow()
        s.commit()
    r = c.get("/institutions/1/exports/postings.csv", headers=h)
    assert r.headers["x-sawazi-held-until-statement"] == "0" and "TJ12ABC34D" in r.text


def test_an_approver_who_changed_an_application_cannot_approve_it(world):
    c, Session, pid = world
    a = apply(c, pid, amount=10_000)
    r = c.patch(f"/institutions/1/loan-applications/{a['id']}", headers=login(c, "approver@a.test"),
                json={"amount_kes": 450_000})
    assert r.status_code == 200
    submit(c, a["id"], who="approver@a.test")
    r = decide(c, a["id"], override_reason="committee reviewed the file")
    assert r.status_code == 403 and "changed" in r.json()["detail"]
    assert decide(c, a["id"], who="approver2@a.test", override_reason="committee reviewed the file").status_code == 200


def test_an_approver_set_up_by_the_preparer_cannot_approve_their_application(world):
    c, Session, pid = world
    admin = login(c, "admin@a.test")
    r = c.post("/institutions/1/users", headers=admin, json={"email": "approver3@a.test", "name": "Third Approver",
                                                             "role": "approver", "password": "admin-knows-it-1"})
    assert r.status_code == 200
    h = {"Authorization": "Bearer " + c.post("/auth/login", json={"email": "approver3@a.test",
                                                                   "password": "admin-knows-it-1"}).json()["token"]}
    c.post("/auth/password", headers=h, json={"current_password": "admin-knows-it-1", "new_password": "own-choice-123"})
    a = apply(c, pid, who="admin@a.test")
    submit(c, a["id"], who="admin@a.test")
    r = c.post(f"/institutions/1/loan-applications/{a['id']}/decide", headers=login(c, "approver3@a.test", "own-choice-123"),
               json={"decision": "approve", "override_reason": "committee reviewed the file"})
    assert r.status_code == 403 and "set up" in r.json()["detail"]
    assert decide(c, a["id"], override_reason="committee reviewed the file").status_code == 200  # an approver the admin did not set up


def test_spreadsheet_formulas_are_neutralised_in_exports():
    from sawazi.api import csv_safe

    assert csv_safe(["=HYPERLINK(\"http://x\")", "+1", "@SUM(A1)", "-cmd", "\tx", "-50.00", "1500.00", "Achieng", 3]) == \
        ["'=HYPERLINK(\"http://x\")", "'+1", "'@SUM(A1)", "'-cmd", "'\tx", "-50.00", "1500.00", "Achieng", 3]
