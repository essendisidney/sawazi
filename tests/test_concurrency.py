"""Simultaneous requests must never allocate the same money twice. Needs real PostgreSQL (separate connections)."""
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
