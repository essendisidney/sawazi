import re
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from sawazi import auth, sms
from sawazi.api import app
from sawazi.models import AuditEvent, Guarantee, Loan, Member, SmsMessage, SmsOptOut
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)

PRODUCT = {"code": "DEV", "name": "Development Loan", "max_amount_kes": 2_000_000, "max_term_months": 48,
           "interest_rate_pct": 12}


class Phone:
    """Stands in for the SMS provider and keeps every text."""

    name = "simulate"

    def __init__(self):
        self.sent = []

    def send(self, phone, text, service_name):
        self.sent.append((phone, text))
        return sms.SendResult("simulated", f"id-{len(self.sent)}", None, "Simulated")

    def link(self):
        return re.search(r"https://sawazi\.test/g/(\S+)", self.sent[-1][1]).group(1)

    def pin(self):
        return re.search(r"\b(\d{6})\b", self.sent[-1][1]).group(1)


@pytest.fixture()
def world(env, monkeypatch):
    c, Session = env
    monkeypatch.setenv("SAWAZI_PUBLIC_URL", "https://sawazi.test")
    phone = Phone()
    app.dependency_overrides[sms.get_provider] = lambda: phone
    joined = date(2020, 1, 1)
    with Session() as s:
        s.add_all([
            Member(id=10, institution_id=1, member_no="M1", name="Achieng <b>Owino</b>", phone="254711000010",
                   joined_on=joined, deposits_cents=10_000_000, gross_pay_cents=15_000_000, net_pay_cents=9_000_000),
            Member(id=11, institution_id=1, member_no="G1", name="Wanjiru Kamau", phone="254711000011",
                   deposits_cents=30_000_000),
            Member(id=12, institution_id=1, member_no="G2", name="Otieno Odhiambo", phone="254711000012",
                   deposits_cents=5_000_000),
            Member(id=13, institution_id=1, member_no="G3", name="In Arrears", phone="254711000013",
                   deposits_cents=50_000_000),
            Member(id=14, institution_id=1, member_no="G4", name="No Balances", phone="254711000014"),
            Member(id=15, institution_id=1, member_no="G5", name="No Phone", deposits_cents=50_000_000),
            Member(id=20, institution_id=2, member_no="X1", name="Other SACCO", phone="254711000020",
                   deposits_cents=50_000_000),
        ])
        s.flush()
        s.add(Loan(id=13, institution_id=1, member_id=13, loan_no="LN13", principal_cents=1, balance_cents=1,
                   installment_cents=1, arrears_cents=1, days_in_arrears=60))
        s.commit()
    pid = c.post("/institutions/1/loan-products", headers=login(c, "admin@a.test"), json=PRODUCT).json()["id"]
    officer = login(c, "credit_officer@a.test")
    a = c.post("/institutions/1/loan-applications", headers=officer,
               json={"member_no": "M1", "product_id": pid, "amount_kes": 300_000, "term_months": 48}).json()
    yield c, Session, phone, a["id"], pid, officer
    app.dependency_overrides.pop(sms.get_provider, None)


def ask(c, officer, app_id, member_no="G1", amount=200_000):
    return c.post(f"/institutions/1/loan-applications/{app_id}/guarantors", headers=officer,
                  json={"member_no": member_no, "amount_kes": amount})


def appraisal_of(c, officer, app_id):
    a = c.get(f"/institutions/1/loan-applications/{app_id}", headers=officer).json()
    return {x["code"]: x for x in a["appraisal"]["checks"]}


def accept(c, phone, token, pin=None):
    c.post(f"/g/{token}/pin")
    return c.post(f"/g/{token}/accept", data={"pin": pin or phone.pin()})


def test_request_accept_and_appraisal(world):
    c, Session, phone, app_id, _, officer = world
    assert appraisal_of(c, officer, app_id)["guarantors"]["status"] == "fail"  # KES 200,000 cover needed
    r = ask(c, officer, app_id)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "requested" and r.json()["sms"] == "simulated"
    to, text = phone.sent[-1]
    assert to == "254711000011" and "guarantee KES 200,000 of their KES 300,000 loan" in text
    token = phone.link()
    assert appraisal_of(c, officer, app_id)["guarantors"]["status"] == "pending"

    with Session() as s:  # neither the link nor anything that opens it is stored
        assert token not in s.scalar(select(SmsMessage.body)) and "[link]" in s.scalar(select(SmsMessage.body))
        assert s.scalar(select(Guarantee.token_hash)) != token

    page = c.get(f"/g/{token}")
    assert page.status_code == 200 and "KES 200,000" in page.text
    low = page.text.lower()
    assert "<b>owino</b>" not in low and "&lt;b&gt;owino&lt;/b&gt;" in low  # names are text, never HTML
    r = accept(c, phone, token)
    assert "You accepted" in r.text
    assert appraisal_of(c, officer, app_id)["guarantors"]["status"] == "pass"
    with Session() as s:
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "guarantee.accept"))
        assert ev.actor_kind == "member" and ev.actor_name == "Wanjiru Kamau" and ev.ip
        assert s.scalar(select(Guarantee.pin_hash)) is None


def test_accepting_needs_the_right_pin(world):
    c, Session, phone, app_id, _, officer = world
    ask(c, officer, app_id)
    token = phone.link()
    assert "Ask for a PIN first" in c.post(f"/g/{token}/accept", data={"pin": "123456"}).text
    c.post(f"/g/{token}/pin")
    pin_text = phone.sent[-1][1]
    assert phone.sent[-1][0] == "254711000011" and "Never share it" in pin_text
    good = phone.pin()
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert "PIN is not right" in c.post(f"/g/{token}/accept", data={"pin": wrong}).text
    assert "Too many wrong PINs" in c.post(f"/g/{token}/accept", data={"pin": good}).text  # locked even if right now
    c.post(f"/g/{token}/pin")
    assert "You accepted" in c.post(f"/g/{token}/accept", data={"pin": phone.pin()}).text


def test_pin_expires_and_is_rationed(world):
    c, Session, phone, app_id, _, officer = world
    ask(c, officer, app_id)
    token = phone.link()
    c.post(f"/g/{token}/pin")
    pin = phone.pin()
    with Session() as s:
        s.scalar(select(Guarantee)).pin_expires_at = auth.utcnow() - timedelta(seconds=1)
        s.commit()
    assert "Ask for a PIN first" in c.post(f"/g/{token}/accept", data={"pin": pin}).text
    c.post(f"/g/{token}/pin")
    c.post(f"/g/{token}/pin")
    assert "Too many PINs" in c.post(f"/g/{token}/pin").text


def test_decline(world):
    c, Session, phone, app_id, _, officer = world
    ask(c, officer, app_id)
    token = phone.link()
    assert "You declined" in c.post(f"/g/{token}/decline").text
    assert "You declined" in c.post(f"/g/{token}/pin").text  # nothing more can happen on this link
    assert appraisal_of(c, officer, app_id)["guarantors"]["status"] == "fail"


def test_capacity_is_checked_when_asked(world):
    c, _, _, app_id, _, officer = world
    r = ask(c, officer, app_id, "G2", 60_000)  # deposits KES 50,000
    assert r.status_code == 422 and "at most KES 50,000" in r.json()["detail"]


def test_two_loans_cannot_take_the_same_deposits(world):
    c, _, phone, app_id, pid, officer = world
    other = c.post("/institutions/1/loan-applications", headers=officer,
                   json={"member_no": "G2", "product_id": pid, "amount_kes": 250_000, "term_months": 48}).json()
    ask(c, officer, app_id, "G1", 200_000)
    first = phone.link()
    ask(c, officer, other["id"], "G1", 200_000)  # G1 has KES 300,000: either request alone fits
    second = phone.link()
    assert "You accepted" in accept(c, phone, first).text
    assert "no longer cover" in accept(c, phone, second).text
    exp = c.get("/institutions/1/members/G1/guarantor-exposure", headers=officer).json()
    assert (exp["deposits_kes"], exp["pledged_kes"], exp["free_kes"]) == (300_000, 200_000, 100_000)


@pytest.mark.parametrize("member_no,code,words", [
    ("M1", 422, "own loan"), ("X1", 404, "not a member"), ("G3", 422, "60 days in arrears"),
    ("G4", 422, "deposits are not known"), ("G5", 422, "no valid phone"), ("NOPE", 404, "not a member")])
def test_who_can_be_a_guarantor(world, member_no, code, words):
    c, _, phone, app_id, _, officer = world
    r = ask(c, officer, app_id, member_no, 1_000)
    assert r.status_code == code and words in r.json()["detail"]
    assert phone.sent == []


def test_duplicates_opt_outs_and_decided_applications(world):
    c, Session, _, app_id, _, officer = world
    ask(c, officer, app_id, "G1", 100_000)
    assert ask(c, officer, app_id, "G1", 50_000).status_code == 409
    with Session() as s:
        s.add(SmsOptOut(institution_id=1, phone="254711000012", source="staff", created_at=auth.utcnow()))
        s.commit()
    r = ask(c, officer, app_id, "G2", 10_000)
    assert r.status_code == 409 and "opted out" in r.json()["detail"]
    c.post(f"/institutions/1/loan-applications/{app_id}/withdraw", headers=officer, params={"note": "changed mind"})
    assert ask(c, officer, app_id, "G2", 10_000).status_code == 409


def test_expired_requests(world):
    c, Session, phone, app_id, _, officer = world
    ask(c, officer, app_id)
    token = phone.link()
    with Session() as s:
        s.scalar(select(Guarantee)).expires_at = auth.utcnow() - timedelta(minutes=1)
        s.commit()
    assert "expired" in c.get(f"/g/{token}").text
    assert appraisal_of(c, officer, app_id)["guarantors"]["status"] == "fail"  # an expired request is not cover


def test_withdrawing_releases_guarantors(world):
    c, Session, phone, app_id, _, officer = world
    ask(c, officer, app_id)
    accept(c, phone, phone.link())
    c.post(f"/institutions/1/loan-applications/{app_id}/withdraw", headers=officer, params={"note": "changed mind"})
    exp = c.get("/institutions/1/members/G1/guarantor-exposure", headers=officer).json()
    assert exp["pledged_kes"] == 0 and exp["free_kes"] == 300_000 and exp["guarantees"] == []
    with Session() as s:
        assert s.scalar(select(Guarantee.status)) == "released"


def test_consent_page_safety(world, monkeypatch):
    c, _, phone, app_id, _, officer = world
    ask(c, officer, app_id)
    r = c.get(f"/g/{phone.link()}")
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    assert "script-src" not in r.headers["content-security-policy"]  # no scripts at all
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert c.get("/g/not-a-real-token").status_code == 404
    monkeypatch.setenv("SAWAZI_PUBLIC_URL", "http://evil.example")  # must be https
    assert ask(c, officer, app_id, "G2", 1_000).status_code == 503


def test_only_loan_staff_ask(world):
    c, _, _, app_id, _, _ = world
    for who in ("viewer@a.test", "accountant@a.test"):
        assert ask(c, login(c, who), app_id).status_code == 403
    assert ask(c, login(c, "credit_officer@b.test"), app_id).status_code == 404


def test_refreshing_after_a_form_never_resends(world):
    c, _, phone, app_id, _, officer = world
    ask(c, officer, app_id)
    token = phone.link()
    r = c.post(f"/g/{token}/pin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/g/{token}?m=pin"
    texts = len(phone.sent)
    assert "PIN sent" in c.get(r.headers["location"]).text  # a refresh is a GET: no new PIN
    assert len(phone.sent) == texts
    assert "PIN sent" not in c.get(f"/g/{token}?m=<script>").text  # only known codes show a message
