"""Phase 2 finish: guarantees released when the loan is repaid, and guarantor consent by USSD."""
from datetime import datetime

import pytest
from sqlalchemy import select

from sawazi.importers import sources
from sawazi.models import AuditEvent, Guarantee, LoanApplication, Member, Transaction
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)
from tests.test_guarantors import Phone, accept, ask, world  # noqa: F401  (world is a fixture)

USSD_TOKEN = "ussd0token0abc"
LOANS_HEADER = b"Loan No,Member No,Principal,Balance,Installment,Arrears\n"


def disbursed_with_guarantor(c, Session, phone, app_id, officer):
    """M1's KES 300,000 loan, guaranteed KES 200,000 by G1, approved, handed over and disbursed as LN900."""
    ask(c, officer, app_id, "G1", 200_000)
    accept(c, phone, phone.link())
    c.post(f"/institutions/1/loan-applications/{app_id}/submit", headers=officer)
    r = c.post(f"/institutions/1/loan-applications/{app_id}/decide", headers=login(c, "approver@a.test"),
               json={"decision": "approve"})
    assert r.json()["status"] == "approved", r.text
    c.post("/institutions/1/loan-applications/export.csv", headers=login(c, "accountant@a.test"))
    with Session() as s:
        sources.import_loans(s, 1, LOANS_HEADER + b"LN900,M1,300000,300000,8000,0\n")
        assert s.get(LoanApplication, app_id).status == "disbursed"


def upload_loans(c, body: bytes):
    r = c.post("/institutions/1/import/loans", headers=login(c, "accountant@a.test"),
               files={"file": ("loans.csv", LOANS_HEADER + body)})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- release on repayment

def test_guarantee_released_when_the_loan_is_repaid(world):
    c, Session, phone, app_id, _, officer = world
    disbursed_with_guarantor(c, Session, phone, app_id, officer)
    exp = c.get("/institutions/1/members/G1/guarantor-exposure", headers=officer).json()
    assert exp["pledged_kes"] == 200_000 and exp["free_kes"] == 100_000
    assert exp["guarantees"][0]["loan_no"] == "LN900" and exp["at_risk_kes"] == 200_000

    assert upload_loans(c, b"LN900,M1,300000,150000,8000,0\n")["guarantees_released"] == 0  # half repaid
    exp = c.get("/institutions/1/members/G1/guarantor-exposure", headers=officer).json()
    assert exp["at_risk_kes"] == 100_000  # their share of what is still owed (information only)
    assert exp["free_kes"] == 100_000  # the full pledge stays committed until the loan is repaid

    assert upload_loans(c, b"LN900,M1,300000,0,8000,0\n")["guarantees_released"] == 1
    exp = c.get("/institutions/1/members/G1/guarantor-exposure", headers=officer).json()
    assert exp["pledged_kes"] == 0 and exp["free_kes"] == 300_000 and exp["guarantees"] == []
    with Session() as s:
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "guarantee.release"))
        assert ev.actor_kind == "system" and ev.note == "loan LN900 repaid"
    assert upload_loans(c, b"LN900,M1,300000,0,8000,0\n")["guarantees_released"] == 0  # once only


def test_a_payment_that_finishes_the_loan_releases_guarantors(world):
    c, Session, phone, app_id, _, officer = world
    disbursed_with_guarantor(c, Session, phone, app_id, officer)
    upload_loans(c, b"LN900,M1,300000,5000,8000,5000\n")  # KES 5,000 left, all in arrears
    with Session() as s:
        s.add(Transaction(institution_id=1, source="mpesa", reference="QXFINAL", txn_time=datetime(2026, 10, 1),
                          amount_cents=500_000, account_ref="M1", payer_phone="254711000010"))
        s.commit()
    r = c.post("/institutions/1/match", headers=login(c, "accountant@a.test")).json()
    assert r["allocated"] == 1 and r["guarantees_released"] == 1
    with Session() as s:
        assert s.scalar(select(Guarantee.status)) == "released"


def test_unpaid_loans_keep_their_guarantors(world):
    c, Session, phone, app_id, _, officer = world
    disbursed_with_guarantor(c, Session, phone, app_id, officer)
    assert upload_loans(c, b"LN900,M1,300000,280000,8000,40000\n")["guarantees_released"] == 0
    with Session() as s:
        assert s.scalar(select(Guarantee.status)) == "accepted"


# ---------------------------------------------------------------- USSD

@pytest.fixture()
def ussd(world, monkeypatch):
    c, Session, phone, app_id, pid, officer = world
    monkeypatch.setenv("SAWAZI_USSD_CALLBACK_TOKEN", USSD_TOKEN)
    with Session() as s:
        for no, idn in [("G1", "23451234"), ("G2", "30009876")]:
            s.scalar(select(Member).where(Member.member_no == no)).id_number = idn
        s.commit()

    def dial(text, session="S1", msisdn="+254711000011", token=USSD_TOKEN):
        return c.post(f"/callbacks/ussd/{token}", data={"sessionId": session, "serviceCode": "*483*77#",
                                                         "phoneNumber": msisdn, "text": text})
    return c, Session, phone, app_id, pid, officer, dial


def test_ussd_accept(ussd):
    c, Session, phone, app_id, _, officer, dial = ussd
    assert dial("").text == "END Sawazi: you have no guarantee requests waiting."
    ask(c, officer, app_id, "G1", 200_000)
    menu = dial("").text
    assert menu.startswith("CON Guarantee requests:\n1. Achieng ") and menu.endswith(" KES 200,000")
    detail = dial("1").text
    assert "KES 200,000 of a KES 300,000 loan" in detail and detail.endswith("1. Accept\n2. Decline")
    assert dial("1*1").text == "CON To confirm, enter the last 4 digits of your ID number:"
    assert dial("1*1*0000").text == "END Those digits do not match. Nothing was accepted."
    r = dial("1*1*1234").text
    assert r.startswith("END Accepted. You now guarantee KES 200,000")
    with Session() as s:
        g = s.scalar(select(Guarantee))
        assert g.status == "accepted" and g.pin_attempts == 1
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "guarantee.accept"))
        assert ev.after["via"] == "ussd" and ev.actor_kind == "member"
    assert dial("").text == "END Sawazi: you have no guarantee requests waiting."


def test_ussd_decline_needs_a_second_yes(ussd):
    c, Session, _, app_id, _, officer, dial = ussd
    ask(c, officer, app_id, "G1", 200_000)
    dial("")
    assert dial("1*2").text.startswith("CON Decline guaranteeing")
    assert dial("1*2*2").text == "END Nothing was changed. Dial again any time."
    assert dial("1*2*1").text == "END You declined. Nothing more is needed."
    with Session() as s:
        assert s.scalar(select(Guarantee.status)) == "declined"


def test_ussd_answer_lands_on_the_request_that_was_shown(ussd):
    """The list changes mid-session (request 1 is declined on the web): "2" must still mean request 2."""
    c, Session, phone, app_id, pid, officer, dial = ussd
    other = c.post("/institutions/1/loan-applications", headers=officer,
                   json={"member_no": "G2", "product_id": pid, "amount_kes": 100_000, "term_months": 24}).json()
    ask(c, officer, app_id, "G1", 50_000)
    first = phone.link()
    ask(c, officer, other["id"], "G1", 60_000)
    assert "1. Achieng" in dial("").text and "2. Otieno O. KES 60,000" in dial("").text
    c.post(f"/g/{first}/decline")  # meanwhile, request 1 is answered elsewhere
    assert dial("1*1*1234").text == "END This request is no longer open. Dial again to see your requests."
    assert "KES 60,000" in dial("2").text  # not shifted onto another request
    assert dial("2*1*1234").text.startswith("END Accepted. You now guarantee KES 60,000 for Otieno O.")


def test_ussd_safety(ussd, monkeypatch):
    c, Session, _, app_id, _, officer, dial = ussd
    ask(c, officer, app_id, "G1", 200_000)
    assert dial("", token="wrong").status_code == 404
    dial("", session="S9")
    assert dial("1*1*1234", session="S9", msisdn="+254711000012").text == "END Session expired. Dial again to see your requests."
    assert dial("7").text == "END Session expired. Dial again to see your requests."  # no list shown in S1 yet
    dial("")
    assert dial("7").text == "END Invalid choice. Dial again to see your requests."
    for _ in range(5):
        dial("", session="S2")
        dial("1*1*9999", session="S2")
    assert dial("1*1*1234", session="S2").text == "END Too many wrong tries. Please contact your SACCO."
    with Session() as s:
        assert s.scalar(select(Guarantee.status)) == "requested"
    monkeypatch.setenv("SAWAZI_USSD_ALLOWED_IPS", "196.201.214.200")
    assert dial("").status_code == 404
    monkeypatch.delenv("SAWAZI_USSD_CALLBACK_TOKEN")
    monkeypatch.delenv("SAWAZI_USSD_ALLOWED_IPS")
    assert dial("").status_code == 404  # closed when not configured


def test_ussd_without_id_number_sends_them_elsewhere(ussd):
    c, Session, _, app_id, _, officer, dial = ussd
    with Session() as s:
        s.scalar(select(Member).where(Member.member_no == "G1")).id_number = None
        s.commit()
    ask(c, officer, app_id, "G1", 200_000)
    dial("")
    assert "ID number is not on record" in dial("1*1").text


def test_ussd_screens_fit(ussd):
    c, _, _, app_id, _, officer, dial = ussd
    ask(c, officer, app_id, "G1", 200_000)
    for text in ["", "1", "1*1", "1*2", "1*1*0000", "1*2*2", "9"]:
        assert len(dial(text).text) <= 182, text


def test_sms_mentions_the_ussd_code_once_there_is_one(ussd, monkeypatch):
    c, _, phone, app_id, _, officer, _ = ussd
    ask(c, officer, app_id, "G1", 100_000)
    assert "dial" not in phone.sent[-1][1]
    monkeypatch.setenv("SAWAZI_USSD_CODE", "*483*77#")
    ask(c, officer, app_id, "G2", 10_000)
    assert "or dial *483*77# to accept or decline" in phone.sent[-1][1]


# ---------------------------------------------------------------- Taifa Mobile's USSD format

from sawazi.api import ussd_input  # noqa: E402


@pytest.mark.parametrize("raw,shortcut,expected", [
    ("", None, ""), ("1*1*4321", None, "1*1*4321"),
    ("100", "100", ""), ("100*1", "100", "1"), ("100*1*1*4321", "100", "1*1*4321"),
    ("1**2*", None, "1*2"),  # empty parts are ignored, as in Taifa's sample handler
    ("1*100", "100", "1*100"),  # only a leading shortcut is stripped
])
def test_ussd_input(raw, shortcut, expected):
    assert ussd_input(raw, shortcut) == expected


def test_taifa_format_get_json_form_and_shared_shortcut(ussd, monkeypatch):
    c, Session, _, app_id, _, officer, _ = ussd
    ask(c, officer, app_id, "G1", 200_000)
    url = f"/callbacks/ussd/{USSD_TOKEN}"
    base = {"MSISDN": "254711000011", "SESSION_ID": "1732612345", "SERVICE_CODE": "*252*100#"}

    r = c.get(url, params={**base, "USSD_STRING": ""})  # GET with a query string
    assert r.headers["content-type"].startswith("text/plain") and r.text.startswith("CON Guarantee requests:")
    assert c.post(url, json={**base, "USSD_STRING": "1"}).text.endswith("1. Accept\n2. Decline")  # JSON
    assert c.post(url, data={**base, "USSD_STRING": "1*1"}).text.startswith("CON To confirm")  # form

    monkeypatch.setenv("SAWAZI_USSD_SHORTCUT", "100")  # shared code *252*100#: "100" leads every path
    assert c.post(url, files={k: (None, v) for k, v in {**base, "USSD_STRING": "100*1"}.items()}).text.endswith(
        "1. Accept\n2. Decline")  # multipart
    r = c.post(url, json={**base, "USSD_STRING": "100*1*1*1234"})
    assert r.text.startswith("END Accepted. You now guarantee KES 200,000")
    with Session() as s:
        assert s.scalar(select(Guarantee.status)) == "accepted"
