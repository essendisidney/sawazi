import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from sawazi import auth, sms
from sawazi.api import app
from sawazi.member_api import weak_pin
from sawazi.models import AuditEvent, Member, StaffUser, MemberCredential, MemberOtp, SmsMessage
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)
from tests.test_guarantors import Phone

PIN = "4826"


@pytest.fixture()
def members(env):
    c, Session = env
    phone = Phone()
    app.dependency_overrides[sms.get_provider] = lambda: phone
    with Session() as s:
        s.add_all([
            Member(id=10, institution_id=1, member_no="M1", name="Achieng Owino", phone="254711000010",
                   deposits_cents=10_000_000),
            Member(id=11, institution_id=1, member_no="M2", name="Otieno Owino", phone="254711000010"),  # shared phone
            Member(id=20, institution_id=2, member_no="B7", name="Achieng Owino", phone="254711000010"),  # other SACCO
            Member(id=12, institution_id=1, member_no="M3", name="Someone Else", phone="254711000099"),
        ])
        s.commit()
    yield c, Session, phone
    app.dependency_overrides.pop(sms.get_provider, None)


def code(phone):
    return re.search(r"code is (\d{6})", phone.sent[-1][1]).group(1)


def sign_up(c, phone, membership=10, pin=PIN, number="0711 000 010"):
    c.post("/m/otp", json={"phone": number})
    v = c.post("/m/otp/verify", json={"phone": number, "code": code(phone)}).json()
    r = c.post("/m/devices", json={"verify_token": v["verify_token"], "membership": membership, "pin": pin})
    assert r.status_code == 200, r.text
    return r.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("pin,ok", [("4826", True), ("907315", True), ("1234", False), ("0000", False),
                                    ("9876", False), ("12", False), ("12ab", False), ("1234567", False)])
def test_weak_pins(pin, ok):
    assert (weak_pin(pin) is None) == ok


def test_sign_in_with_sms_code_then_pin(members):
    c, Session, phone = members
    first = c.post("/m/otp", json={"phone": "0711 000 010"}).json()
    unknown = c.post("/m/otp", json={"phone": "0799 999 999"}).json()
    assert first == unknown  # never reveals who is a member
    assert len(phone.sent) == 1 and phone.sent[0][0] == "254711000010"  # and texts no stranger
    v = c.post("/m/otp/verify", json={"phone": "0711000010", "code": code(phone)}).json()
    assert {(m["institution"], m["member_no"], m["first_name"]) for m in v["memberships"]} == {
        ("SACCO A", "M1", "Achieng"), ("SACCO A", "M2", "Otieno"), ("SACCO B", "B7", "Achieng")}
    assert c.post("/m/devices", json={"verify_token": v["verify_token"], "membership": 10, "pin": "1111"}).status_code == 422
    r = c.post("/m/devices", json={"verify_token": v["verify_token"], "membership": 10, "pin": PIN}).json()
    assert r["member"] == {"member_no": "M1", "name": "Achieng Owino", "institution": "SACCO A"}
    assert c.get("/m/me", headers=bearer(r["session_token"])).json()["member_no"] == "M1"
    again = c.post("/m/devices", json={"verify_token": v["verify_token"], "membership": 11, "pin": PIN})
    assert again.status_code == 400  # one code, one phone set up
    r = c.post("/m/login", json={"device_token": r["device_token"], "pin": PIN})
    assert r.status_code == 200 and r.json()["session_token"].startswith("mbr_")
    with Session() as s:
        stored = s.scalar(select(SmsMessage.body))
        assert "######" in stored and code(phone) not in stored  # the code is never kept
        assert s.scalar(select(MemberCredential.pin_hash)).startswith("scrypt$")


def test_a_code_cannot_set_up_someone_elses_membership(members):
    c, _, phone = members
    c.post("/m/otp", json={"phone": "0711000010"})
    v = c.post("/m/otp/verify", json={"phone": "0711000010", "code": code(phone)}).json()
    r = c.post("/m/devices", json={"verify_token": v["verify_token"], "membership": 12, "pin": PIN})  # M3, other phone
    assert r.status_code == 400


def test_wrong_codes_expiry_and_rate_limit(members):
    c, Session, phone = members
    c.post("/m/otp", json={"phone": "0711000010"})
    for left in (4, 3, 2, 1):
        r = c.post("/m/otp/verify", json={"phone": "0711000010", "code": "000000" if code(phone) != "000000" else "111111"})
        assert r.status_code == 400 and f"{left} tries left" in r.json()["detail"]
    c.post("/m/otp/verify", json={"phone": "0711000010", "code": "999999" if code(phone) != "999999" else "888888"})
    assert "expired" in c.post("/m/otp/verify", json={"phone": "0711000010", "code": code(phone)}).json()["detail"]
    c.post("/m/otp", json={"phone": "0711000010"})
    with Session() as s:
        s.scalars(select(MemberOtp).order_by(MemberOtp.id.desc())).first().expires_at = auth.utcnow() - timedelta(seconds=1)
        s.commit()
    assert "expired" in c.post("/m/otp/verify", json={"phone": "0711000010", "code": code(phone)}).json()["detail"]
    c.post("/m/otp", json={"phone": "0711000010"})
    c.post("/m/otp", json={"phone": "0711000010"})
    assert len(phone.sent) == 3  # at most 3 codes an hour per number


def test_pin_lockout(members):
    c, _, phone = members
    dev = sign_up(c, phone)["device_token"]
    for left in (4, 3, 2, 1):
        r = c.post("/m/login", json={"device_token": dev, "pin": "9999"})
        assert r.status_code == 401 and f"{left} tries left" in r.json()["detail"]
    assert "Too many wrong PINs" in c.post("/m/login", json={"device_token": dev, "pin": "9999"}).json()["detail"]
    r = c.post("/m/login", json={"device_token": dev, "pin": PIN})
    assert r.status_code == 423  # even the right PIN waits out the lock
    sign_up(c, phone, pin="5937")  # an SMS code resets the PIN (forgotten PIN)
    assert c.post("/m/login", json={"device_token": dev, "pin": "5937"}).status_code == 200


def test_member_and_staff_tokens_never_cross(members):
    c, _, phone = members
    member = bearer(sign_up(c, phone)["session_token"])
    assert c.get("/institutions/1/dashboard", headers=member).status_code == 401
    assert c.get("/auth/me", headers=member).status_code == 401
    assert c.get("/m/me", headers=login(c, "admin@a.test")).status_code == 401
    assert c.get("/m/me").status_code == 401


def test_staff_can_switch_off_app_access(members):
    c, Session, phone = members
    r = sign_up(c, phone)
    out = c.post("/institutions/1/members/M1/app-access/revoke", headers=login(c, "admin@a.test"),
                 params={"note": "member reported a lost phone"})
    assert out.json()["devices_revoked"] == 1
    assert c.get("/m/me", headers=bearer(r["session_token"])).status_code == 401
    assert c.post("/m/login", json={"device_token": r["device_token"], "pin": PIN}).status_code == 401
    assert c.post("/institutions/1/members/M1/app-access/revoke", headers=login(c, "accountant@a.test"),
                  params={"note": "x"}).status_code == 403
    assert c.post("/institutions/2/members/B7/app-access/revoke", headers=login(c, "admin@a.test"),
                  params={"note": "x"}).status_code == 404
    with Session() as s:
        assert s.scalar(select(AuditEvent.action).where(AuditEvent.action == "member_app.revoke"))


def test_staff_see_app_access_and_must_give_a_reason(members):
    c, Session, phone = members
    admin = login(c, "admin@a.test")
    assert c.get("/institutions/1/members/M1/app-access", headers=admin).json()["has_pin"] is False
    sign_up(c, phone)
    a = c.get("/institutions/1/members/M1/app-access", headers=admin).json()
    assert a["has_pin"] and len(a["devices"]) == 1 and a["devices"][0]["revoked_at"] is None and a["locked_until"] is None
    assert "pin_hash" not in str(a) and "token" not in str(a)
    assert c.post("/institutions/1/members/M1/app-access/revoke", headers=admin, params={"note": ""}).status_code == 422
    assert c.get("/institutions/1/members/M1/app-access", headers=login(c, "accountant@a.test")).status_code == 403
    assert c.get("/institutions/2/members/B7/app-access", headers=admin).status_code in (403, 404)
    assert c.get("/institutions/1/members/NOPE/app-access", headers=admin).status_code == 404


# ---------------------------------------------------------------- the member's own data

from datetime import date, datetime  # noqa: E402

from sawazi.models import Allocation, Guarantee, Institution, Loan, LoanApplication, Transaction  # noqa: E402

PRODUCT = {"code": "DEV", "name": "Development Loan", "max_amount_kes": 2_000_000, "max_term_months": 48,
           "interest_rate_pct": 12}


@pytest.fixture()
def signed_in(members, monkeypatch):
    c, Session, phone = members
    monkeypatch.setenv("SAWAZI_PUBLIC_URL", "https://sawazi.test")
    with Session() as s:
        s.get(Member, 10).joined_on = date(2020, 1, 1)
        s.add_all([Loan(id=10, institution_id=1, member_id=10, loan_no="LN10", product="Development Loan",
                        principal_cents=20_000_000, balance_cents=8_000_000, installment_cents=500_000,
                        arrears_cents=100_000, days_in_arrears=12),
                   Loan(id=11, institution_id=1, member_id=11, loan_no="LN11", principal_cents=1,
                        balance_cents=999_999, installment_cents=1, arrears_cents=0, days_in_arrears=0)])
        s.flush()
        s.add(Transaction(id=10, institution_id=1, source="mpesa", reference="QX10", txn_time=datetime(2026, 9, 5),
                          amount_cents=600_000, status="allocated", member_id=10))
        s.flush()
        s.add(Allocation(institution_id=1, transaction_id=10, target="loan_arrears", loan_id=10, amount_cents=100_000))
        s.get(Institution, 1).paybill = "522900"
        s.commit()
    token = sign_up(c, phone)["session_token"]
    return c, Session, phone, bearer(token)


def test_overview_shows_only_my_money(signed_in):
    c, _, _, me = signed_in
    o = c.get("/m/overview", headers=me).json()
    assert o["deposits_kes"] == 100_000 and [x["loan_no"] for x in o["loans"]] == ["LN10"]  # not M2's, same phone
    assert o["loans"][0]["pay"] == {"paybill": "522900", "account": "LN10"}
    assert o["pay_deposits"] == {"paybill": "522900", "account": "M1"}
    assert o["payments"][0]["reference"] == "QX10" and o["payments"][0]["split"] == [{"to": "loan_arrears", "kes": 1000}]


def test_guarantee_requests_answered_in_the_app(signed_in):
    c, Session, phone, me = signed_in
    pid = c.post("/institutions/1/loan-products", headers=login(c, "admin@a.test"), json=PRODUCT).json()["id"]
    officer = login(c, "credit_officer@a.test")
    a = c.post("/institutions/1/loan-applications", headers=officer,
               json={"member_no": "M3", "product_id": pid, "amount_kes": 50_000, "term_months": 12}).json()
    c.post(f"/institutions/1/loan-applications/{a['id']}/guarantors", headers=officer,
           json={"member_no": "M1", "amount_kes": 40_000})
    g = c.get("/m/guarantees", headers=me).json()
    assert g["waiting"][0]["for"] == "Someone E." and g["waiting"][0]["kes"] == 40_000
    assert c.post(f"/m/guarantees/{g['waiting'][0]['id']}/answer", headers=me, json={"answer": "accept"}).json() == \
        {"status": "accepted"}
    g = c.get("/m/guarantees", headers=me).json()
    assert g["waiting"] == [] and g["pledged_kes"] == 40_000 and g["can_still_guarantee_kes"] == 60_000
    assert c.post(f"/m/guarantees/{g['giving'][0]['id']}/answer", headers=me, json={"answer": "decline"}).status_code == 409
    with Session() as s:
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "guarantee.accept"))
        assert ev.after["via"] == "app" and ev.actor_kind == "member"
        officer_id = s.scalar(select(StaffUser.id).where(StaffUser.email == "credit_officer@a.test"))
        other = Guarantee(institution_id=1, application_id=a["id"], guarantor_member_id=12, amount_cents=1, status="requested",
                          phone="254711000099", token_hash="x" * 64, expires_at=auth.utcnow() + timedelta(days=1),
                          requested_by_user_id=officer_id, requested_at=auth.utcnow())
        s.add(other)
        s.commit()
        oid = other.id
    assert c.post(f"/m/guarantees/{oid}/answer", headers=me, json={"answer": "accept"}).status_code == 404  # not mine


def test_apply_from_the_app(signed_in):
    c, Session, _, me = signed_in
    pid = c.post("/institutions/1/loan-products", headers=login(c, "admin@a.test"), json=PRODUCT).json()["id"]
    assert [p["name"] for p in c.get("/m/products", headers=me).json()] == ["Development Loan"]
    check = c.post("/m/loan-check", headers=me, json={"product_id": pid, "amount_kes": "150000", "term_months": 24}).json()
    assert check["outcome"] in ("passes", "incomplete", "fails") and check["instalment_kes"] > 0
    assert all(x["code"] != "guarantors" for x in check["checks"])
    bad = c.post("/m/applications", headers=me, json={"product_id": pid, "amount_kes": "150000", "term_months": 24,
                                                       "guarantors": ["NOPE"]})
    assert bad.status_code == 422
    r = c.post("/m/applications", headers=me, json={"product_id": pid, "amount_kes": "150000", "term_months": 24,
                                                     "purpose": "Dairy", "guarantors": ["m3", "M3"]}).json()
    assert r["status"] == "draft"
    with Session() as s:
        a = s.scalar(select(LoanApplication))
        assert (a.source, a.prepared_by_user_id, a.nominated_guarantors) == ("member_app", None, ["M3"])
    staff = c.get(f"/institutions/1/loan-applications/{a.id}", headers=login(c, "credit_officer@a.test")).json()
    assert staff["source"] == "member_app" and staff["nominated_guarantors"] == ["M3"]
    c.post(f"/institutions/1/loan-applications/{a.id}/submit", headers=login(c, "approver@a.test"))
    r = c.post(f"/institutions/1/loan-applications/{a.id}/decide", headers=login(c, "approver@a.test"),
               json={"decision": "approve", "override_reason": "testing maker-checker on app loans"})
    assert r.status_code == 403  # whoever submitted a member's application is its maker
    c.post("/m/applications", headers=me, json={"product_id": pid, "amount_kes": "1000", "term_months": 6})
    third = c.post("/m/applications", headers=me, json={"product_id": pid, "amount_kes": "1000", "term_months": 6})
    assert third.status_code == 409  # at most two waiting at once
    assert [x["ref"] for x in c.get("/m/applications", headers=me).json()][-1] == f"SWZ-{a.id}"


# ---------------------------------------------------------------- the app files

APP_DIR = __import__("pathlib").Path(__file__).parent.parent / "sawazi" / "app"


def test_app_is_served_with_strict_headers(members):
    c, _, phone = members
    r = c.get("/app/")
    assert r.status_code == 200 and 'id="app"' in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "worker-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert c.get("/app/manifest.webmanifest").json()["start_url"] == "/app/"
    assert c.get("/app/sw.js").status_code == 200
    me = sign_up(c, phone)
    assert c.get("/m/overview", headers=bearer(me["session_token"])).headers["cache-control"] == "no-store"


def test_app_page_has_no_inline_script_and_builds_text_only():
    page = (APP_DIR / "index.html").read_text(encoding="utf-8")
    assert re.search(r"<script(?![^>]*\bsrc=)", page) is None
    js = (APP_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js
    sw = (APP_DIR / "sw.js").read_text(encoding="utf-8")
    assert '"/m/' not in sw.split("SHELL")[1].split("]")[0]  # member data never in the offline cache


def test_app_strings_in_both_languages():
    js = (APP_DIR / "app.js").read_text(encoding="utf-8")
    en = js.split("  en: {")[1].split("\n  sw: {")[0]
    sw = js.split("\n  sw: {")[1].split("\n};")[0]
    keys = lambda block: set(re.findall(r"(\w+): [\"{]", block))  # noqa: E731
    assert keys(en) == keys(sw)


@pytest.mark.parametrize("name", ["app.js", "sw.js"])
def test_app_scripts_parse(name):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js not installed")
    r = subprocess.run([node, "--check", str(APP_DIR / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
