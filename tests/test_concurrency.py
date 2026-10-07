"""Simultaneous requests must never allocate the same money twice. Needs real PostgreSQL (separate connections)."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from sqlalchemy import func, select

from sawazi.models import Allocation, ExceptionItem, Institution, Loan, Member, Transaction
from tests.conftest import TEST_DB_URL
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)

pytestmark = pytest.mark.skipif(not TEST_DB_URL, reason="needs SAWAZI_TEST_DB_URL (PostgreSQL)")
N = 6


@pytest.fixture()
def seeded(env, monkeypatch):
    c, Session = env
    monkeypatch.setenv("SAWAZI_DARAJA_CALLBACK_TOKEN", "tok123")
    with Session() as s:
        s.get(Institution, 1).paybill = "522900"
        s.add(Member(id=10, institution_id=1, member_no="UT00104", name="Achieng Owino", phone="254711000001"))
        s.flush()
        s.add(Loan(id=10, institution_id=1, member_id=10, loan_no="LN1", principal_cents=10_000_000,
                   balance_cents=5_000_000, installment_cents=500_000, arrears_cents=300_000, days_in_arrears=20))
        s.add(Transaction(id=10, institution_id=1, source="bank", reference="FT1", txn_time=datetime(2026, 9, 5),
                          amount_cents=300_000, status="suspense"))
        s.flush()
        s.add(ExceptionItem(id=10, institution_id=1, kind="suspense", transaction_id=10, amount_cents=300_000,
                            detail="unclear"))
        s.commit()
    return c, Session


def test_same_suspense_cleared_by_several_staff_at_once(seeded):
    c, Session = seeded
    heads = [login(c, "accountant@a.test") for _ in range(N)]
    with ThreadPoolExecutor(N) as pool:
        codes = list(pool.map(lambda h: c.post("/exceptions/10/resolve", headers=h,
                                               json={"member_no": "UT00104"}).status_code, heads))
    assert sorted(codes) == [200] + [404] * (N - 1)
    with Session() as s:
        assert s.scalar(select(func.sum(Allocation.amount_cents))) == 300_000
        assert s.get(Loan, 10).arrears_cents == 0


def test_same_callback_delivered_several_times_at_once(seeded):
    c, Session = seeded
    body = {"TransID": "TJ1", "TransTime": "20261006093015", "TransAmount": "3000.00", "BusinessShortCode": "522900",
            "BillRefNumber": "UT00104", "MSISDN": "254711000001", "FirstName": "Achieng"}
    with ThreadPoolExecutor(N) as pool:
        results = list(pool.map(lambda _: c.post("/callbacks/c2b/tok123/confirmation", json=body), range(N)))
    assert all(r.status_code == 200 and r.json()["ResultCode"] == 0 for r in results)
    with Session() as s:
        assert s.scalar(select(func.count()).where(Transaction.reference == "TJ1")) == 1
        assert s.scalar(select(func.sum(Allocation.amount_cents))) == 300_000  # allocated once
        assert s.get(Loan, 10).arrears_cents == 0


def test_two_acceptances_at_once_cannot_overcommit_a_guarantor(env, monkeypatch):
    """G1 has KES 300,000 of deposits and is asked for KES 200,000 on two loans; both accept at the same moment."""
    from sawazi import sms
    from sawazi.api import app
    from tests.test_guarantors import PRODUCT, Phone

    c, Session = env
    monkeypatch.setenv("SAWAZI_PUBLIC_URL", "https://sawazi.test")
    phone = Phone()
    app.dependency_overrides[sms.get_provider] = lambda: phone
    with Session() as s:
        s.add_all([Member(id=10, institution_id=1, member_no="A1", name="A One", phone="254711000010"),
                   Member(id=11, institution_id=1, member_no="A2", name="A Two", phone="254711000011"),
                   Member(id=12, institution_id=1, member_no="G1", name="G One", phone="254711000012",
                          deposits_cents=30_000_000)])
        s.commit()
    pid = c.post("/institutions/1/loan-products", headers=login(c, "admin@a.test"), json=PRODUCT).json()["id"]
    officer = login(c, "credit_officer@a.test")
    tokens, pins = [], []
    for applicant in ("A1", "A2"):
        a = c.post("/institutions/1/loan-applications", headers=officer,
                   json={"member_no": applicant, "product_id": pid, "amount_kes": 300_000, "term_months": 48}).json()
        c.post(f"/institutions/1/loan-applications/{a['id']}/guarantors", headers=officer,
               json={"member_no": "G1", "amount_kes": 200_000})
        tokens.append(phone.link())
        c.post(f"/g/{tokens[-1]}/pin")
        pins.append(phone.pin())
    from sawazi import guarantors
    real = guarantors.free_capacity

    def slow_capacity(s, m):  # widen the race window so both acceptances overlap for certain
        free = real(s, m)
        time.sleep(0.4)
        return free
    monkeypatch.setattr(guarantors, "free_capacity", slow_capacity)
    with ThreadPoolExecutor(2) as pool:
        pages = list(pool.map(lambda tp: c.post(f"/g/{tp[0]}/accept", data={"pin": tp[1]}).text, zip(tokens, pins)))
    app.dependency_overrides.pop(sms.get_provider, None)
    assert sum("You accepted" in p for p in pages) == 1
    assert sum("no longer cover" in p for p in pages) == 1
    from sawazi.models import Guarantee
    with Session() as s:
        assert sorted(s.scalars(select(Guarantee.status))) == ["accepted", "requested"]
