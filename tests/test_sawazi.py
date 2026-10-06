from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sawazi.db import Base, get_session
from sawazi.engine.checkoff import reconcile_checkoff
from sawazi.engine.collections import build_queue, priority, stage_for
from sawazi.engine.matching import MemberIndex, find_member, run_matching
from sawazi.importers import sources
from sawazi.importers.common import norm_phone, to_cents
from sawazi.models import Allocation, ExceptionItem, Institution, Loan, Member, Transaction

DATA = Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture()
def s():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng, expire_on_commit=False)()
    session.add(Institution(id=1, name="Test SACCO", paybill="111222"))
    session.add_all([
        Member(id=1, institution_id=1, member_no="UT00104", name="Achieng Owino", phone="254711000001", id_number="23456789"),
        Member(id=2, institution_id=1, member_no="UT00134", name="Kamau Njoroge", phone="254711000002"),
        Member(id=3, institution_id=1, member_no="UT00200", name="Grace Wafula", phone="254711000003"),
    ])
    session.add_all([
        Loan(id=1, institution_id=1, member_id=1, loan_no="LN1", principal_cents=10_000_000, balance_cents=5_000_000,
             installment_cents=500_000, arrears_cents=300_000, days_in_arrears=20),
        Loan(id=2, institution_id=1, member_id=3, loan_no="LN2", principal_cents=10_000_000, balance_cents=8_000_000,
             installment_cents=400_000, arrears_cents=0, days_in_arrears=0, repays_via="checkoff"),
    ])
    session.commit()
    yield session
    session.close()


def tx(s, ref, amount, phone=None, name=None, account=None, narrative=None, source="mpesa"):
    t = Transaction(institution_id=1, source=source, reference=ref, txn_time=datetime(2026, 9, 5, 10),
                    amount_cents=amount, payer_phone=phone, payer_name=name, account_ref=account, narrative=narrative)
    s.add(t)
    s.commit()
    return t


# ---------------------------------------------------------------- parsing

def test_parsing_helpers():
    assert to_cents("1,250.50") == 125050
    assert to_cents("(300.00)") == -30000
    assert to_cents("") == 0
    assert norm_phone("0712 345 678") == "254712345678"
    assert norm_phone("+254-712-345678") == "254712345678"
    assert norm_phone("12345") is None


def test_mpesa_import_is_idempotent_and_skips_outgoing(s):
    csv = (
        "Receipt No.,Completion Time,Details,Transaction Status,Paid In,Withdrawn,Other Party Info,A/C No.\n"
        "R1,05-09-2026 10:00:00,Pay Bill Online,Completed,500.00,,254711000001 - ACHIENG OWINO,UT00104\n"
        "R2,05-09-2026 10:05:00,Charge,Completed,,-33.00,,\n"
        "R3,05-09-2026 10:06:00,Pay Bill Online,Failed,700.00,,254711000002 - KAMAU,UT00134\n"
    )
    first = sources.import_mpesa_statement(s, 1, csv)
    again = sources.import_mpesa_statement(s, 1, csv)
    assert first.created == 1 and again.created == 0 and again.skipped_duplicates == 1
    t = s.scalar(select(Transaction).where(Transaction.reference == "R1"))
    assert t.payer_phone == "254711000001" and t.payer_name == "ACHIENG OWINO"


# ---------------------------------------------------------------- matching

@pytest.mark.parametrize("account,phone,narrative,expected_member,method", [
    ("UT00104", None, None, 1, "member_no"),
    (" ut-00104 ", None, None, 1, "member_no"),
    ("LN1", None, None, 1, "loan_no"),
    ("23456789", None, None, 1, "id_number"),
    ("loan", "254711000003", None, 3, "phone"),
    (None, None, "CASH DEP UT00200 GRACE WAFULA", 3, "member_no"),
])
def test_find_member_strong_evidence(s, account, phone, narrative, expected_member, method):
    t = tx(s, "X" + method, 1000, phone=phone, account=account, narrative=narrative)
    cand, _ = find_member(MemberIndex(s, 1), t)
    assert cand.member_id == expected_member and cand.method == method and cand.confidence >= 85


def test_valid_but_wrong_member_number_is_caught(s):
    # Achieng (UT00104) mistypes her own number as UT00134, which belongs to Kamau.
    t = tx(s, "TYPO", 500_000, phone="254711000001", name="ACHIENG OWINO", account="UT00134")
    cand, reason = find_member(MemberIndex(s, 1), t)
    assert cand.member_id == 1 and cand.method == "ref_conflict" and cand.confidence < 85
    assert "typo" in reason.lower()


def test_unknown_payer_goes_to_suspense_with_reason(s):
    tx(s, "UNK", 250_000, phone="254799999999", name="SOMEONE ELSE", account="for mama")
    res = run_matching(s, 1)
    assert res["suspense"] == 1 and res["allocated"] == 0
    e = s.scalar(select(ExceptionItem).where(ExceptionItem.kind == "suspense"))
    assert "no member number" in e.detail


def test_allocation_order_arrears_then_installment_then_deposits(s):
    tx(s, "PAY", 1_000_000, account="UT00104")  # KES 10,000
    run_matching(s, 1)
    allocs = {a.target: a.amount_cents for a in s.scalars(select(Allocation))}
    assert allocs == {"loan_arrears": 300_000, "loan_installment": 500_000, "deposits": 200_000}
    loan = s.get(Loan, 1)
    assert loan.arrears_cents == 0 and loan.days_in_arrears == 0 and loan.balance_cents == 4_200_000


def test_double_payment_flagged(s):
    tx(s, "D1", 500_000, phone="254711000001", account="UT00104")
    t2 = Transaction(institution_id=1, source="mpesa", reference="D2", txn_time=datetime(2026, 9, 5, 10, 4),
                     amount_cents=500_000, payer_phone="254711000001", account_ref="UT00104")
    s.add(t2)
    s.commit()
    run_matching(s, 1)
    assert s.scalar(select(ExceptionItem).where(ExceptionItem.kind == "possible_double_payment")) is not None


# ---------------------------------------------------------------- check-off

def test_checkoff_short_missing_and_name_only_lines(s):
    sources.import_checkoff_schedule(s, 1, "County", "2026-09",
                                     "Member No,Amount\nUT00104,5000\nUT00134,4000\nUT00200,3000\n")
    sources.import_checkoff_remittance(s, 1, "County", "2026-09",
                                       "Member No,Payroll Name,Amount\n"
                                       "UT00104,ACHIENG OWINO,2500\n"        # short
                                       ",GRACE WAFULA,3000\n"                # name only, exact
                                       ",NOBODY KNOWN,1500\n")               # unidentified; UT00134 missing
    r = reconcile_checkoff(s, 1, "County", "2026-09")
    assert (r["short"], r["missing"], r["exact"], r["unidentified"]) == (1, 1, 1, 1)
    assert r["expected_kes"] == 12000 and r["remitted_kes"] == 7000
    # Re-running does not duplicate exceptions or transactions.
    reconcile_checkoff(s, 1, "County", "2026-09")
    assert len(list(s.scalars(select(ExceptionItem).where(ExceptionItem.kind.like("checkoff_%"))))) == 3
    assert len(list(s.scalars(select(Transaction).where(Transaction.source == "checkoff")))) == 2


# ---------------------------------------------------------------- collections

def test_collections_stages_and_priority(s):
    assert stage_for(3).channel == "sms"
    assert stage_for(45).channel == "guarantor_notice"
    assert stage_for(200).name == "loss"
    loan = s.get(Loan, 1)
    assert 0 < priority(loan) <= 100
    res = build_queue(s, 1)
    assert res["queued"] == 1 and "first_month" in res["by_stage"]


# ---------------------------------------------------------------- end to end

def test_api_end_to_end_on_sample_data(monkeypatch):
    monkeypatch.setenv("SAWAZI_API_KEY", "platform-test-key")
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, expire_on_commit=False)

    def override():
        sess = Session()
        try:
            yield sess
        finally:
            sess.close()

    from sawazi.api import app
    app.dependency_overrides[get_session] = override
    c = TestClient(app)
    pk = {"X-API-Key": "platform-test-key"}
    iid = c.post("/institutions", json={"name": "Ufanisi Teachers SACCO", "paybill": "522900"}, headers=pk).json()["id"]
    r = c.post(f"/institutions/{iid}/admin", headers=pk,
               json={"email": "admin@ufanisi.test", "name": "Admin", "role": "admin", "password": "admin-pass-123"})
    assert r.status_code == 200, r.text
    tok = c.post("/auth/login", json={"email": "admin@ufanisi.test", "password": "admin-pass-123"}).json()["token"]
    admin = {"Authorization": f"Bearer {tok}"}
    c.post(f"/institutions/{iid}/users", headers=admin,
           json={"email": "acc@ufanisi.test", "name": "Accountant", "role": "accountant", "password": "acc-pass-1234"})
    tok = c.post("/auth/login", json={"email": "acc@ufanisi.test", "password": "acc-pass-1234"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    for kind, f in [("members", "members.csv"), ("loans", "loans.csv"), ("mpesa", "mpesa_statement.csv"),
                    ("bank", "bank_statement.csv")]:
        r = c.post(f"/institutions/{iid}/import/{kind}", files={"file": (f, (DATA / f).read_bytes())})
        assert r.status_code == 200 and r.json()["created"] > 0, r.text
    r = c.post(f"/institutions/{iid}/import/checkoff_schedule", params={"employer": "Tumaini Schools Ltd", "period": "2026-09"},
               files={"file": ("s.csv", (DATA / "checkoff_schedule_tumaini.csv").read_bytes())})
    assert r.status_code == 200
    c.post(f"/institutions/{iid}/import/checkoff_remittance", params={"employer": "Tumaini Schools Ltd", "period": "2026-09"},
           files={"file": ("r.csv", (DATA / "checkoff_remittance_tumaini.csv").read_bytes())})
    assert c.post(f"/institutions/{iid}/checkoff/reconcile", params={"employer": "Tumaini Schools Ltd", "period": "2026-09"}).status_code == 200
    m = c.post(f"/institutions/{iid}/match").json()
    assert m["auto_match_rate"] > 85
    c.post(f"/institutions/{iid}/collections/queue")
    assert len(c.get(f"/institutions/{iid}/reminders").json()) > 0

    # Clear one suspense item by hand.
    susp = c.get(f"/institutions/{iid}/exceptions", params={"kind": "suspense"}).json()
    assert susp
    r = c.post(f"/exceptions/{susp[0]['id']}/resolve", json={"member_no": "UT00001", "note": "member called in"})
    assert r.status_code == 200 and r.json()["allocations"]

    dash = c.get(f"/institutions/{iid}/dashboard").json()
    assert dash["portfolio"]["active_loans"] > 0
    csv_out = c.get(f"/institutions/{iid}/exports/postings.csv").text
    assert csv_out.startswith("date,source,reference,member_no")
    app.dependency_overrides.clear()
