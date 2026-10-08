import csv
import io
from datetime import date

import pytest
from sqlalchemy import select

from sawazi import returns
from sawazi.models import AuditEvent, Loan, Member
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


@pytest.mark.parametrize("d,label,end,due", [
    (date(2026, 9, 30), "2026 Q3", date(2026, 9, 30), date(2026, 10, 15)),
    (date(2026, 7, 1), "2026 Q3", date(2026, 9, 30), date(2026, 10, 15)),
    (date(2026, 12, 31), "2026 Q4", date(2026, 12, 31), date(2027, 1, 15)),
    (date(2027, 2, 14), "2027 Q1", date(2027, 3, 31), date(2027, 4, 15)),
])
def test_quarter_and_deadline(d, label, end, due):
    assert returns.quarter_of(d) == (label, end, due)


@pytest.fixture()
def book(env):
    c, Session = env
    with Session() as s:
        s.add_all([Member(id=10 + i, institution_id=1, member_no=f"M{i}", name=f"Member {i}") for i in range(1, 7)])
        s.add(Member(id=99, institution_id=2, member_no="X1", name="Other SACCO"))
        s.flush()
        for i, (days, bal, interest, known) in enumerate([(0, 1_000_000, 0, True), (10, 1_000_000, 5_000, True),
                                                          (45, 1_000_001, 20_000, True), (200, 1_000_000, 30_000, True),
                                                          (400, 1_000_000, 40_000, False)], start=1):
            s.add(Loan(id=10 + i, institution_id=1, member_id=10 + i, loan_no=f"L{i}", product="DEV",
                       principal_cents=2_000_000, balance_cents=bal, installment_cents=1, arrears_cents=interest,
                       interest_arrears_cents=interest, arrears_breakdown=known, days_in_arrears=days))
        s.add(Loan(id=16, institution_id=1, member_id=16, loan_no="L6", principal_cents=1, balance_cents=0,
                   installment_cents=1, arrears_cents=0, days_in_arrears=0, status="closed"))  # repaid: not in Form 4
        s.add(Loan(id=99, institution_id=2, member_id=99, loan_no="X", principal_cents=1, balance_cents=9_000_000,
                   installment_cents=1, arrears_cents=0, days_in_arrears=500))
        s.commit()
    return c, Session


def test_schedule_by_class(book):
    c, _ = book
    c.post("/institutions/1/snapshots", headers=login(c, "accountant@a.test"), params={"as_of": "2026-09-30"})
    r = c.get("/institutions/1/returns/form4", headers=login(c, "accountant@a.test"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["quarter"], body["quarter_end"], body["due"], body["is_quarter_end"]) == (
        "2026 Q3", "2026-09-30", "2026-10-15", True)
    got = {x["class"]: (x["loans"], x["balance_kes"], x["provision_pct"], x["provision_kes"]) for x in body["classes"]}
    assert got == {"performing": (1, 10_000, 1, 100), "watch": (1, 10_000, 5, 500),
                   "substandard": (1, 10_000.01, 25, 2_500.01),  # rounded up, never down
                   "doubtful": (1, 10_000, 50, 5_000), "loss": (1, 10_000, 100, 10_000)}
    t = body["totals"]
    assert t["loans"] == 5 and t["balance_kes"] == 50_000.01 and t["provision_kes"] == 18_100.01
    assert t["npl_balance_kes"] == 30_000.01 and t["npl_pct"] == 60
    # interest to suspend: substandard and doubtful only; L5's interest is not known (no breakdown in the export)
    assert t["interest_to_suspend_kes"] == 500 and body["interest_known"] is True


def test_loan_level_csv_traces_every_total(book):
    c, Session = book
    r = c.get("/institutions/1/returns/form4.csv", headers=login(c, "accountant@a.test"))
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert "Not the official form." in rows[0][0]
    lines = [dict(zip(rows[1], x)) for x in rows[2:]]
    assert [x["loan_no"] for x in lines] == ["L5", "L4", "L3", "L2", "L1"]  # worst first
    assert sum(float(x["provision"]) for x in lines) == pytest.approx(18_100.01)
    assert {x["class"] for x in lines} == {"performing", "watch", "substandard", "doubtful", "loss"}
    with Session() as s:
        assert s.scalar(select(AuditEvent.action).where(AuditEvent.action == "returns.form4_download"))


@pytest.mark.parametrize("who", ["viewer@a.test", "credit_officer@a.test", "approver@a.test"])
def test_who_may_see_returns(book, who):
    c, _ = book
    assert c.get("/institutions/1/returns/form4", headers=login(c, who)).status_code == 403


def test_returns_stay_in_their_institution(book):
    c, _ = book
    body = c.get("/institutions/1/returns/form4", headers=login(c, "admin@a.test")).json()
    assert body["totals"]["loans"] == 5  # institution 2's loan is not here
    assert c.get("/institutions/2/returns/form4", headers=login(c, "admin@a.test")).status_code == 404
