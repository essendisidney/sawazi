from datetime import date

from sawazi.engine import appraisal
from sawazi.models import Loan, Member
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)
from tests.test_member_app import bearer, members, sign_up  # noqa: F401  (members is a fixture)

ADV = appraisal.Product(100_000, 50_000_000, 12, 1200, deposits_multiplier_pct=0, min_membership_months=0,
                        max_arrears_days=0, one_third_rule=False, guarantor_cover="none", dividend_advance_pct=50)


def facts(dividend, open_advances=0):
    return appraisal.MemberFacts(None, None, None, None, dividend, open_advances)


def test_advance_is_a_share_of_the_last_dividend_and_one_at_a_time():
    on = date(2026, 10, 13)
    unknown = appraisal.appraise(500_000, 6, ADV, facts(None), [], [], on)
    assert unknown.outcome == "incomplete" and unknown.limits["dividend"] is None  # unknown is never a pass
    ok = appraisal.appraise(1_000_000, 6, ADV, facts(2_000_000), [], [], on)
    assert ok.outcome == "passes" and ok.max_eligible_cents == 1_000_000
    over = appraisal.appraise(1_000_100, 6, ADV, facts(2_000_000), [], [], on)
    assert over.outcome == "fails" and "allows up to KES 10,000" in over.checks[-1].message
    again = appraisal.appraise(500_000, 6, ADV, facts(2_000_000, open_advances=1), [], [], on)
    assert again.outcome == "fails" and again.checks[-1].code == "one_advance"
    behind = appraisal.appraise(500_000, 6, ADV, facts(2_000_000), [appraisal.ExistingLoan("L1", 100, 3)], [], on)
    assert behind.outcome == "fails"  # a defaulter gets no advance


def test_dividend_advance_end_to_end(members):
    c, Session, phone = members
    admin, officer = login(c, "admin@a.test"), login(c, "credit_officer@a.test")
    up = c.post("/institutions/1/import/member_balances", headers=admin,
                files={"file": ("bal.csv", b"Member No,Last Dividend\nM1,20000\n")})
    assert up.status_code == 200, up.text
    with Session() as s:
        assert s.get(Member, 10).last_dividend_cents == 2_000_000
        s.add(Loan(id=50, institution_id=1, member_id=10, loan_no="L50", principal_cents=1, balance_cents=1,
                   installment_cents=1))
        s.commit()
    p = c.post("/institutions/1/loan-products", headers=admin, json={
        "code": "DIV", "name": "Dividend Advance", "max_amount_kes": 500_000, "max_term_months": 12,
        "interest_rate_pct": 12, "deposits_multiplier": 0, "min_membership_months": 0, "max_arrears_days": 0,
        "one_third_rule": False, "guarantor_cover": "none", "dividend_advance_pct": 50}).json()
    assert p["dividend_advance_pct"] == 50

    def what_if(kes):
        return c.post("/institutions/1/appraisal/what-if", headers=officer,
                      json={"member_no": "M1", "product_id": p["id"], "amount_kes": kes, "term_months": 6}).json()

    assert what_if(10_000)["outcome"] == "passes" and what_if(10_001)["outcome"] == "fails"

    tok = bearer(sign_up(c, phone)["session_token"])
    mine = [x for x in c.get("/m/products", headers=tok).json() if x["id"] == p["id"]][0]
    assert mine["dividend_advance"] and mine["advance_up_to_kes"] == 10_000
    top = c.post("/m/applications", headers=tok, json={"product_id": p["id"], "amount_kes": 5000, "term_months": 6,
                                                       "top_up_loan_no": "L50", "guarantors": []})
    assert top.status_code == 422 and "cannot be topped up" in top.json()["detail"]

    a = c.post("/institutions/1/loan-applications", headers=officer,
               json={"member_no": "M1", "product_id": p["id"], "amount_kes": 8000, "term_months": 6}).json()
    assert c.post(f"/institutions/1/loan-applications/{a['id']}/submit", headers=officer).status_code == 200
    second = what_if(1000)
    assert second["outcome"] == "fails" and any(x["code"] == "one_advance" for x in second["checks"])
    own = c.get(f"/institutions/1/loan-applications/{a['id']}", headers=officer).json()
    assert all(x["code"] != "one_advance" for x in own["appraisal"]["checks"])  # it never blocks itself
