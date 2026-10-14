from datetime import date, timedelta

from sqlalchemy import select

from sawazi import auth, boardpack
from sawazi.models import (AuditEvent, Institution, LoanApplication, LoanProduct, Loan, Member)
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


def _loan(i, days, status="active"):
    return Loan(id=i, institution_id=1, member_id=1, loan_no=f"L{i}", principal_cents=1, balance_cents=10_000_00,
                installment_cents=1_000_00, arrears_cents=1_000_00 if days else 0, days_in_arrears=days, status=status)


def test_roll_rates_follow_each_loan_between_two_dates(env):
    c, Session = env
    with Session() as s:
        s.add(Member(id=1, institution_id=1, member_no="M1", name="Achieng Owino"))
        s.flush()
        s.add_all([_loan(1, 0), _loan(2, 10), _loan(3, 20), _loan(4, 45)])
        s.commit()
        boardpack.take_snapshot(s, 1, date(2026, 8, 31))
        s.commit()
        for lid, days, status in ((1, 5, "active"), (2, 0, "active"), (3, 40, "active"), (4, 0, "closed")):
            ln = s.get(Loan, lid)
            ln.days_in_arrears, ln.status = days, status
        s.add(_loan(5, 0))
        s.commit()
        boardpack.take_snapshot(s, 1, date(2026, 9, 30))
        s.commit()
    r = c.get("/institutions/1/roll-rates", headers=login(c, "viewer@a.test")).json()
    assert r["dates"] == ["2026-09-30", "2026-08-31"]
    rows = {x["from"]: x for x in r["rates"]["rows"]}
    assert rows["current"]["to"]["1-30"] == 1 and rows["current"]["rolled_worse_pct"] == 100.0
    assert rows["1-30"]["loans"] == 2 and rows["1-30"]["to"]["current"] == 1 and rows["1-30"]["to"]["31-60"] == 1
    assert (rows["1-30"]["rolled_worse_pct"], rows["1-30"]["cured_pct"]) == (50.0, 50.0)
    assert rows["31-60"]["to"]["closed"] == 1 and rows["31-60"]["cured_pct"] == 100.0
    assert r["rates"]["new_loans"] == 1


def test_reference_help_and_turnaround(env):
    c, Session = env
    now = auth.utcnow()
    with Session() as s:
        s.get(Institution, 1).paybill = "522900"
        s.add(Member(id=1, institution_id=1, member_no="M1", name="ACHIENG OWINO", phone="254711000001"))
        s.flush()
        s.add(_loan(1, 0))
        for _ in range(2):
            s.add(AuditEvent(institution_id=1, at=now - timedelta(days=3), actor_kind="user", actor_name="Clerk",
                             action="suspense.clear", after={"member_no": "M1"}))
        p = LoanProduct(institution_id=1, code="DEV", name="Dev", min_amount_cents=1, max_amount_cents=10**9,
                        max_term_months=12, interest_rate_bps=1200, interest_method="reducing",
                        deposits_multiplier_pct=0, min_membership_months=0, max_arrears_days=30, one_third_rule=False,
                        guarantor_cover="none", min_guarantors=0, created_at=now, updated_at=now)
        s.add(p)
        s.flush()
        s.add_all([LoanApplication(institution_id=1, member_id=1, product_id=p.id, amount_cents=1, term_months=6,
                                   status="disbursed", created_at=now - timedelta(days=10),
                                   submitted_at=now - timedelta(days=9), decided_at=now - timedelta(days=7),
                                   exported_at=now - timedelta(days=6), disbursed_at=now - timedelta(days=5)),
                   LoanApplication(institution_id=1, member_id=1, product_id=p.id, amount_cents=5_000_00, term_months=6,
                                   status="submitted", created_at=now - timedelta(days=6),
                                   submitted_at=now - timedelta(days=5))])
        s.commit()
    viewer = login(c, "viewer@a.test")
    help_ = c.get("/institutions/1/reference-coaching", headers=viewer).json()
    assert help_[0]["times"] == 2 and help_[0]["account"] == "L1"
    assert help_[0]["message"].startswith("Dear Achieng,") and "Paybill 522900, account L1." in help_[0]["message"]

    t = c.get("/institutions/1/turnaround", headers=viewer).json()
    assert t["median_days"]["submit_to_decision"] == 2.0 and t["median_days"]["submit_to_money"] == 4.0
    assert [x["status"] for x in t["stuck"]] == ["submitted"] and t["stuck"][0]["waiting_days"] == 5.0
    with Session() as s:
        assert s.scalar(select(LoanApplication.id).where(LoanApplication.status == "submitted")) == t["stuck"][0]["id"]
