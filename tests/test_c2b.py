import base64
import json

import httpx
import pytest
from sqlalchemy import select

from sawazi import daraja
from sawazi.models import Allocation, ExceptionItem, Institution, Loan, Member, MpesaCallback, Transaction
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)

TOKEN = "c2b0token0abc123"


def payload(**over):
    body = {"TransactionType": "Pay Bill", "TransID": "TJ12ABC34D", "TransTime": "20261006093015",
            "TransAmount": "5000.00", "BusinessShortCode": "522900", "BillRefNumber": "UT00104",
            "InvoiceNumber": "", "OrgAccountBalance": "", "ThirdPartyTransID": "",
            "MSISDN": "254711000001", "FirstName": "Achieng", "MiddleName": "", "LastName": "Owino"}
    return {**body, **over}


# ---------------------------------------------------------------- parsing

def test_parse_callback():
    p = daraja.parse(payload())
    assert (p.trans_id, p.shortcode, p.amount_cents, p.account_ref) == ("TJ12ABC34D", "522900", 500_000, "UT00104")
    assert p.txn_time.isoformat() == "2026-10-06T09:30:15"
    assert (p.payer_phone, p.payer_name) == ("254711000001", "ACHIENG OWINO")


@pytest.mark.parametrize("msisdn,phone", [
    ("254711000001", "254711000001"), ("0711000001", "254711000001"), ("2547 ***** 001", None),
    ("2547****1001", None), ("a3f1" * 16, None), ("", None)])
def test_masked_or_hashed_msisdn_is_not_trusted(msisdn, phone):
    assert daraja.parse(payload(MSISDN=msisdn)).payer_phone == phone


@pytest.mark.parametrize("bad", [{"TransID": ""}, {"BusinessShortCode": None}, {"TransAmount": "0"},
                                 {"TransAmount": "abc"}, {"TransTime": "06/10/2026"}])
def test_bad_callbacks_rejected_by_parser(bad):
    with pytest.raises(daraja.BadCallback):
        daraja.parse(payload(**bad))


def test_callback_url_rules():
    daraja.check_callback_url("https://sawazi.example.co.ke/callbacks/c2b/abc/confirmation")
    for url in ["http://x.co.ke/callbacks/c2b/a/confirmation", "https://x.co.ke/mpesa/confirmation",
                "https://x.co.ke/callbacks/c2b/SQLtoken/confirmation"]:
        with pytest.raises(ValueError):
            daraja.check_callback_url(url)


# ---------------------------------------------------------------- Daraja client

def test_register_urls_request():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/oauth/v1/generate":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": "3599"})
        return httpx.Response(200, json={"ResponseCode": "0", "ResponseDescription": "success"})

    c = daraja.DarajaClient("ck", "cs", "sandbox", transport=httpx.MockTransport(handler))
    out = c.register_urls("600000", "https://h.co.ke/callbacks/c2b/t/confirmation",
                          "https://h.co.ke/callbacks/c2b/t/validation")
    assert out["ResponseCode"] == "0"
    tok, reg = calls
    assert str(tok.url).startswith("https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials")
    assert tok.headers["Authorization"] == "Basic " + base64.b64encode(b"ck:cs").decode()
    assert reg.url.path == "/mpesa/c2b/v2/registerurl" and reg.headers["Authorization"] == "Bearer tok"
    assert json.loads(reg.content) == {"ShortCode": "600000", "ResponseType": "Completed",
                                       "ConfirmationURL": "https://h.co.ke/callbacks/c2b/t/confirmation",
                                       "ValidationURL": "https://h.co.ke/callbacks/c2b/t/validation"}


def test_simulate_is_sandbox_only():
    c = daraja.DarajaClient("ck", "cs", "production", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(RuntimeError, match="sandbox"):
        c.simulate("600000", 100, "254708374149", "UT00104")


# ---------------------------------------------------------------- callbacks

@pytest.fixture()
def paybill(env, monkeypatch):
    c, Session = env
    monkeypatch.setenv("SAWAZI_DARAJA_CALLBACK_TOKEN", TOKEN)
    monkeypatch.delenv("SAWAZI_DARAJA_ALLOWED_IPS", raising=False)
    with Session() as s:
        s.get(Institution, 1).paybill = "522900"
        s.get(Institution, 2).paybill = "888777"
        s.add(Member(id=10, institution_id=1, member_no="UT00104", name="Achieng Owino", phone="254711000001"))
        s.add(Loan(id=10, institution_id=1, member_id=10, loan_no="LN1", principal_cents=10_000_000,
                   balance_cents=5_000_000, installment_cents=500_000, arrears_cents=300_000, days_in_arrears=20))
        s.commit()
    return c, Session


def confirm(c, body=None, token=TOKEN):
    return c.post(f"/callbacks/c2b/{token}/confirmation", json=body or payload())


def txns(Session):
    with Session() as s:
        return list(s.scalars(select(Transaction).order_by(Transaction.id)))


def test_confirmation_records_and_matches_in_real_time(paybill):
    c, Session = paybill
    r = confirm(c)
    assert r.status_code == 200 and r.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    [t] = txns(Session)
    assert (t.institution_id, t.source, t.reference, t.amount_cents) == (1, "mpesa", "TJ12ABC34D", 500_000)
    assert t.status == "allocated" and t.member_id == 10
    with Session() as s:
        assert sum(a.amount_cents for a in s.scalars(select(Allocation))) == 500_000
        cb = s.scalar(select(MpesaCallback))
        assert (cb.kind, cb.transaction_id, cb.payload["BillRefNumber"]) == ("confirmation", t.id, "UT00104")


def test_repeat_callback_is_idempotent(paybill):
    c, Session = paybill
    confirm(c)
    confirm(c)
    assert len(txns(Session)) == 1
    with Session() as s:
        assert sum(a.amount_cents for a in s.scalars(select(Allocation))) == 500_000  # not allocated twice
        assert len(list(s.scalars(select(MpesaCallback)))) == 2  # but both are on record
    confirm(c, payload(TransAmount="9000.00"))  # same TransID, different amount: suspicious
    with Session() as s:
        assert s.scalar(select(ExceptionItem).where(ExceptionItem.kind == "c2b_mismatch")).severity == "high"
    assert len(txns(Session)) == 1


def test_unknown_account_goes_to_suspense(paybill):
    c, Session = paybill
    confirm(c, payload(TransID="TJ99", BillRefNumber="XYZ", MSISDN="2547****999", FirstName="Stranger", LastName=""))
    [t] = txns(Session)
    assert t.status == "suspense"
    with Session() as s:
        assert s.scalar(select(ExceptionItem).where(ExceptionItem.kind == "suspense")).transaction_id == t.id


def test_payments_go_to_the_institution_that_owns_the_paybill(paybill):
    c, Session = paybill
    confirm(c, payload(BusinessShortCode="888777"))
    confirm(c, payload(TransID="TJ2", BusinessShortCode="111111"))  # nobody's paybill: acknowledged, not stored
    assert [(t.institution_id, t.reference) for t in txns(Session)] == [(2, "TJ12ABC34D")]


def test_bad_callbacks_still_acknowledged(paybill):
    c, Session = paybill
    r = confirm(c, {"TransID": "X"})
    assert r.status_code == 200 and r.json()["ResultCode"] == 0
    assert txns(Session) == []


def test_validation_always_accepts_and_moves_nothing(paybill):
    c, Session = paybill
    r = c.post(f"/callbacks/c2b/{TOKEN}/validation", json=payload(BillRefNumber="NOT-A-MEMBER"))
    assert r.json() == {"ResultCode": 0, "ResultDesc": "Accepted"}
    assert txns(Session) == []
    with Session() as s:
        assert s.scalar(select(MpesaCallback)).kind == "validation"


def test_callback_secrets_and_ip_allowlist(paybill, monkeypatch):
    c, Session = paybill
    assert confirm(c, token="wrong").status_code == 404
    monkeypatch.setenv("SAWAZI_DARAJA_ALLOWED_IPS", "196.201.214.200, 196.201.214.206")
    assert confirm(c).status_code == 404  # the test client is not Safaricom
    monkeypatch.setenv("SAWAZI_DARAJA_ALLOWED_IPS", "testclient")
    assert confirm(c).status_code == 200
    monkeypatch.delenv("SAWAZI_DARAJA_CALLBACK_TOKEN")
    assert confirm(c).status_code == 404  # closed when not configured
    assert len(txns(Session)) == 1


def test_matching_failure_does_not_lose_the_payment(paybill, monkeypatch):
    c, Session = paybill

    def broken(*_):
        raise RuntimeError("boom")
    monkeypatch.setattr("sawazi.api.run_matching", broken)
    assert confirm(c).json()["ResultCode"] == 0
    [t] = txns(Session)
    assert t.status == "unmatched"  # the next match run picks it up


def test_matching_runs_under_the_institution_lock(paybill, monkeypatch):
    c, _ = paybill
    import sawazi.api as api
    held = []
    monkeypatch.setattr(api, "run_matching", lambda s, iid: held.append(api._match_locks[iid].locked()) or {})
    confirm(c)
    c.post("/institutions/1/match", headers=login(c, "accountant@a.test"))
    assert held == [True, True]


# ---------------------------------------------------------------- the statement confirms the callbacks

STATEMENT_HEADER = ("Receipt No.,Completion Time,Initiation Time,Details,Transaction Status,Paid In,Withdrawn,"
                    "Balance,Balance Confirmed,Reason Type,Other Party Info,Linked Transaction ID,A/C No.\n")


def statement(receipt="TJ12ABC34D", amount="5000.00", other="254711000001 - ACHIENG OWINO"):
    return (STATEMENT_HEADER + f"{receipt},06-10-2026 09:30:15,06-10-2026 09:30:15,Pay Bill Online,Completed,"
            f"{amount},,100000.00,true,Pay Bill Online,{other},,UT00104\n").encode()


def upload(c, content):
    r = c.post("/institutions/1/import/mpesa", headers=login(c, "accountant@a.test"),
               files={"file": ("statement.csv", content)})
    assert r.status_code == 200, r.text
    return r.json()


def test_statement_confirms_callback_and_fills_masked_phone(paybill):
    c, Session = paybill
    confirm(c, payload(MSISDN="2547****001"))
    viewer = login(c, "viewer@a.test")
    assert len(c.get("/institutions/1/c2b/unconfirmed", headers=viewer, params={"older_than_hours": 0}).json()) == 1

    res = upload(c, statement())
    assert res["created"] == 0 and res["skipped_duplicates"] == 1 and res["callbacks_confirmed"] == 1
    [t] = txns(Session)
    assert t.payer_phone == "254711000001"
    assert c.get("/institutions/1/c2b/unconfirmed", headers=viewer, params={"older_than_hours": 0}).json() == []
    assert upload(c, statement()).get("callbacks_confirmed") is None  # nothing left to confirm


def test_statement_disagreeing_with_callback_is_flagged(paybill):
    c, Session = paybill
    confirm(c)
    res = upload(c, statement(amount="500.00"))
    assert res["callbacks_mismatched"] == 1
    with Session() as s:
        e = s.scalar(select(ExceptionItem).where(ExceptionItem.kind == "c2b_mismatch"))
        assert e.severity == "high" and "KES 500.00" in e.detail and e.member_id == 10
        assert "statement says" in s.scalar(select(MpesaCallback)).statement_mismatch


def test_statement_first_then_callback(paybill):
    c, Session = paybill
    upload(c, statement())
    confirm(c)
    assert len(txns(Session)) == 1  # the callback for a receipt we already have adds no new payment
    viewer = login(c, "viewer@a.test")
    assert c.get("/institutions/1/c2b/unconfirmed", headers=viewer, params={"older_than_hours": 0}).json() == []
    confirm(c, payload(TransAmount="7000.00"))  # disagrees with the statement: stays unconfirmed, flagged
    assert len(c.get("/institutions/1/c2b/unconfirmed", headers=viewer, params={"older_than_hours": 0}).json()) == 1
