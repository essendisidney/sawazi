from datetime import date

import pytest
from sqlalchemy import select

from sawazi.engine import appraisal as ap
from sawazi.engine.appraisal import ExistingLoan, Guarantee, MemberFacts, Product
from sawazi.models import AuditEvent, Loan, Member
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)

ON = date(2026, 10, 7)
PRODUCT = Product(min_amount_cents=100_000, max_amount_cents=200_000_000, max_term_months=48, interest_rate_bps=1200)
GOOD = MemberFacts(joined_on=date(2020, 1, 1), deposits_cents=10_000_000, gross_pay_cents=9_000_000,
                   net_pay_cents=4_000_000)


def run(amount=5_000_000, term=12, product=PRODUCT, member=GOOD, loans=(), guarantees=()):
    return ap.appraise(amount, term, product, member, list(loans), list(guarantees), ON)


def check(result, code):
    return next(c for c in result.checks if c.code == code)


# ---------------------------------------------------------------- instalment estimate

def test_instalment_reducing_and_flat():
    assert ap.instalment(10_000_000, 12, 1200, "reducing") == 888_488  # KES 100,000 at 12% for 12 months
    assert ap.instalment(10_000_000, 12, 1200, "flat") == 933_334
    assert ap.instalment(1_200_000, 12, 0, "reducing") == 100_000


@pytest.mark.parametrize("method", ap.INTEREST_METHODS)
@pytest.mark.parametrize("budget", [1, 99_999, 500_000, 1_234_567])
def test_principal_for_is_the_exact_inverse(method, budget):
    p = ap.principal_for(budget, 24, 1450, method)
    assert ap.instalment(p, 24, 1450, method) <= budget < ap.instalment(p + 1, 24, 1450, method)


# ---------------------------------------------------------------- the rules

def test_a_good_application_passes():
    r = run()
    assert r.outcome == "passes" and {c.status for c in r.checks} == {"pass"}
    assert r.required_cover_cents == 0  # KES 50,000 against KES 100,000 deposits


def test_product_limits():
    assert check(run(amount=50_000), "amount").status == "fail"
    assert check(run(term=60), "term").status == "fail"


def test_membership_period():
    recent = MemberFacts(date(2026, 6, 1), GOOD.deposits_cents, GOOD.gross_pay_cents, GOOD.net_pay_cents)
    c = check(run(member=recent), "membership")
    assert c.status == "fail" and "4 months" in c.message
    unknown = MemberFacts(None, GOOD.deposits_cents, GOOD.gross_pay_cents, GOOD.net_pay_cents)
    r = run(member=unknown)
    assert check(r, "membership").status == "unknown" and r.outcome == "incomplete"  # unknown is never a pass


def test_existing_arrears():
    assert check(run(loans=[ExistingLoan("LN1", 0, 45)]), "arrears").status == "fail"
    r = run(loans=[ExistingLoan("LN1", 0, 10)])
    assert check(r, "arrears").status == "warn" and r.outcome == "passes"  # a warning for the approver to weigh


def test_deposits_multiplier_counts_other_loans():
    loans = [ExistingLoan("LN1", 5_000_000, 0)]  # KES 50,000 outstanding; 3x KES 100,000 deposits = KES 300,000
    r = run(amount=25_000_000, loans=loans)
    assert check(r, "deposits").status == "pass" and r.limits["deposits"] == 25_000_000
    assert check(run(amount=25_000_100, loans=loans), "deposits").status == "fail"


def test_one_third_rule():
    # gross 90,000: take-home must stay at least 30,000; net 40,000 leaves 10,000 a month for the new loan
    r = run(amount=10_000_000, term=12)  # instalment 8,884.88
    assert check(r, "one_third").status == "pass"
    c = check(run(amount=12_000_000, term=12), "one_third")  # instalment about 10,662
    assert c.status == "fail" and "KES 30,000" in c.message
    assert ap.instalment(run().limits["affordability"], 12, 1200, "reducing") <= 1_000_000
    no_pay = MemberFacts(GOOD.joined_on, GOOD.deposits_cents, None, None)
    c = check(run(member=no_pay), "one_third")
    assert c.status == "unknown" and "no payslip" in c.message


def test_guarantor_cover_above_deposits():
    big = 30_000_000  # KES 300,000 against KES 100,000 deposits: KES 200,000 must be guaranteed
    asked = [Guarantee("A", 15_000_000, "accepted"), Guarantee("B", 5_000_000, "requested")]
    r = run(amount=big, term=48, guarantees=asked)
    assert r.required_cover_cents == 20_000_000 and r.accepted_cover_cents == 15_000_000
    assert check(r, "guarantors").status == "pending" and r.outcome == "incomplete"
    declined = [Guarantee("A", 15_000_000, "accepted"), Guarantee("B", 5_000_000, "declined")]
    c = check(run(amount=big, term=48, guarantees=declined), "guarantors")
    assert c.status == "fail" and "KES 50,000 more cover" in c.message
    enough = [Guarantee("A", 15_000_000, "accepted"), Guarantee("B", 5_000_000, "accepted")]
    assert check(run(amount=big, term=48, guarantees=enough), "guarantors").status == "pass"


def test_full_cover_and_minimum_guarantors():
    product = Product(100_000, 200_000_000, 48, 1200, guarantor_cover="full", min_guarantors=2)
    one = [Guarantee("A", 5_000_000, "accepted")]
    c = check(run(product=product, guarantees=one), "guarantors")
    assert c.status == "fail" and "1 more guarantor" in c.message


def test_fail_outranks_unknown_and_max_eligible():
    no_pay = MemberFacts(None, GOOD.deposits_cents, None, None)
    r = run(amount=999_999_999, member=no_pay)
    assert r.outcome == "fails" and r.max_eligible_cents is None  # a needed figure is unknown
    r = run()
    assert r.max_eligible_cents == min(r.limits.values())


# ---------------------------------------------------------------- API

PRODUCT_IN = {"code": "dev", "name": "Development Loan", "max_amount_kes": 2_000_000, "max_term_months": 48,
              "interest_rate_pct": 12}


def test_products_api(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    r = c.post("/institutions/1/loan-products", headers=admin, json=PRODUCT_IN)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["code"] == "DEV" and p["deposits_multiplier"] == 3 and p["min_membership_months"] == 6
    assert p["max_arrears_days"] == 30 and p["one_third_rule"] and p["guarantor_cover"] == "above_deposits"
    assert c.post("/institutions/1/loan-products", headers=admin, json=PRODUCT_IN).status_code == 409
    assert c.post("/institutions/1/loan-products", headers=admin,
                  json={**PRODUCT_IN, "code": "X", "min_amount_kes": 5_000_000}).status_code == 422
    assert c.post("/institutions/1/loan-products", headers=login(c, "accountant@a.test"),
                  json={**PRODUCT_IN, "code": "Y"}).status_code == 403
    r = c.put(f"/institutions/1/loan-products/{p['id']}", headers=admin, json={**PRODUCT_IN, "interest_rate_pct": 14})
    assert r.json()["interest_rate_pct"] == 14
    with Session() as s:
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "loan_product.update"))
        assert ev.before == {"interest_rate_pct": 12} and ev.after == {"interest_rate_pct": 14}
    assert c.get("/institutions/1/loan-products", headers=login(c, "viewer@a.test")).json()[0]["code"] == "DEV"
    assert c.get("/institutions/2/loan-products", headers=admin).status_code == 404
    assert c.put(f"/institutions/2/loan-products/{p['id']}", headers=admin, json=PRODUCT_IN).status_code == 404


def test_what_if(env):
    c, Session = env
    with Session() as s:
        s.add(Member(id=10, institution_id=1, member_no="M1", name="Achieng Owino", joined_on=date(2026, 8, 1),
                     deposits_cents=10_000_000))
        s.add(Member(id=20, institution_id=2, member_no="M9", name="Other SACCO"))
        s.flush()
        s.add(Loan(id=10, institution_id=1, member_id=10, loan_no="LN1", principal_cents=1, balance_cents=5_000_000,
                   installment_cents=1, arrears_cents=0, days_in_arrears=0))
        s.commit()
    pid = c.post("/institutions/1/loan-products", headers=login(c, "admin@a.test"), json=PRODUCT_IN).json()["id"]
    h = login(c, "viewer@a.test")
    r = c.post("/institutions/1/appraisal/what-if", headers=h,
               json={"member_no": "M1", "product_id": pid, "amount_kes": 200000, "term_months": 24})
    assert r.status_code == 200, r.text
    body = r.json()
    status = {x["code"]: x["status"] for x in body["checks"]}
    assert status["membership"] == "fail" and status["deposits"] == "pass" and status["one_third"] == "unknown"
    assert body["outcome"] == "fails" and body["max_eligible_kes"] is None
    assert c.post("/institutions/1/appraisal/what-if", headers=h,
                  json={"member_no": "M9", "product_id": pid, "amount_kes": 1, "term_months": 1}).status_code == 404
