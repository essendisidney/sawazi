import random
from datetime import date, datetime

import pytest
from sqlalchemy import select

from sawazi.engine import allocation as al
from sawazi.engine.allocation import ExcessBucket, LoanState, Rules
from sawazi.engine.matching import run_matching
from sawazi.importers import sources
from sawazi.models import Allocation, AuditEvent, Loan, Member, Transaction
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


def loan(id, arrears=0, penalty=0, interest=0, balance=1_000_000, inst=50_000, dpd=0, disbursed="2025-01-01",
         breakdown=None):
    known = bool(penalty or interest) if breakdown is None else breakdown
    return LoanState(id, f"LN{id}", balance, inst, arrears, penalty, interest, dpd, disbursed, known)


def split(lines):
    return [(x.target, x.loan_id, x.amount_cents) for x in lines]


# ---------------------------------------------------------------- the plan

def test_defaults_without_breakdown_match_original_behaviour():
    loans = [loan(1, arrears=30_000, dpd=20), loan(2, arrears=10_000, dpd=40)]
    assert split(al.plan(200_000, loans, Rules())) == [
        ("loan_arrears", 2, 10_000), ("loan_arrears", 1, 30_000),  # most overdue first
        ("loan_installment", 2, 50_000), ("loan_installment", 1, 50_000), ("deposits", None, 60_000)]
    assert loans[0].arrears_cents == 0 and loans[0].days_in_arrears == 0 and loans[0].balance_cents == 920_000


def test_penalty_interest_principal_order():
    ln = loan(1, arrears=30_000, penalty=2_000, interest=8_000, dpd=20)
    assert split(al.plan(25_000, [ln], Rules())) == [
        ("loan_penalty", 1, 2_000), ("loan_interest", 1, 8_000), ("loan_principal", 1, 15_000)]
    assert (ln.penalty_arrears_cents, ln.interest_arrears_cents, ln.arrears_cents) == (0, 0, 5_000)
    assert ln.days_in_arrears == 20  # still behind


def test_known_breakdown_with_nothing_left_but_principal():
    """Once penalty and interest are paid, what remains is principal, not unknown arrears."""
    ln = loan(1, arrears=30_000, dpd=20, breakdown=True)
    assert split(al.plan(10_000, [ln], Rules())) == [("loan_principal", 1, 10_000)]
    ln = loan(1, arrears=30_000, penalty=2_000, interest=8_000, dpd=20)
    al.plan(10_000, [ln], Rules())  # clears penalty and interest
    assert split(al.plan(5_000, [ln], Rules())) == [("loan_principal", 1, 5_000)]


def test_institution_chooses_a_different_order():
    ln = loan(1, arrears=30_000, penalty=2_000, interest=8_000, dpd=20)
    rules = Rules(arrears_order=["interest", "principal", "penalty"])
    assert split(al.plan(35_000, [ln], rules))[:3] == [
        ("loan_interest", 1, 8_000), ("loan_principal", 1, 20_000), ("loan_penalty", 1, 2_000)]


def test_partial_payment_stops_inside_a_part():
    ln = loan(1, arrears=30_000, penalty=2_000, interest=8_000, dpd=20)
    assert split(al.plan(5_000, [ln], Rules())) == [("loan_penalty", 1, 2_000), ("loan_interest", 1, 3_000)]
    assert (ln.penalty_arrears_cents, ln.interest_arrears_cents, ln.arrears_cents) == (0, 5_000, 25_000)


@pytest.mark.parametrize("order,first", [("most_overdue_first", 2), ("oldest_loan_first", 1),
                                         ("largest_arrears_first", 3)])
def test_loan_order(order, first):
    loans = [loan(1, arrears=1_000, dpd=10, disbursed="2023-01-01"),
             loan(2, arrears=2_000, dpd=90, disbursed="2025-01-01"),
             loan(3, arrears=9_000, dpd=30, disbursed="2024-01-01")]
    assert al.plan(500, loans, Rules(loan_order=order))[0].loan_id == first


def test_named_loan_is_served_first_whatever_the_order():
    loans = [loan(1, arrears=1_000, dpd=90), loan(2, arrears=1_000, dpd=5)]
    assert al.plan(500, loans, Rules(), preferred_loan_id=2)[0].loan_id == 2


def test_skip_current_installment():
    rules = Rules(pay_current_installment=False)
    assert split(al.plan(80_000, [loan(1, arrears=30_000, dpd=10)], rules)) == [
        ("loan_arrears", 1, 30_000), ("deposits", None, 50_000)]


def test_shares_percent_split_keeps_every_cent():
    rules = Rules(excess=[ExcessBucket("shares", percent=20), ExcessBucket("deposits")])
    assert split(al.plan(10_001, [], rules)) == [("shares", None, 2_000), ("deposits", None, 8_001)]


def test_shares_fixed_amount_first():
    rules = Rules(excess=[ExcessBucket("shares", max_cents=100_000), ExcessBucket("deposits")])
    assert split(al.plan(250_000, [], rules)) == [("shares", None, 100_000), ("deposits", None, 150_000)]
    assert split(al.plan(60_000, [], rules)) == [("shares", None, 60_000)]


def test_loan_paid_off_never_overpaid():
    ln = loan(1, arrears=5_000, balance=7_000, inst=50_000, dpd=10)
    assert split(al.plan(100_000, [ln], Rules())) == [
        ("loan_arrears", 1, 5_000), ("loan_installment", 1, 2_000), ("deposits", None, 93_000)]
    assert ln.balance_cents == 0


def test_every_cent_placed_under_any_rules():
    rng = random.Random(7)
    for _ in range(600):
        loans = []
        for i in range(rng.randint(0, 4)):
            arrears = rng.choice([0, rng.randint(1, 200_000)])
            pen = rng.randint(0, arrears // 3)
            loans.append(loan(i + 1, arrears=arrears, penalty=pen, interest=rng.randint(0, arrears - pen),
                              balance=rng.randint(1, 500_000), inst=rng.randint(0, 80_000), dpd=rng.randint(0, 200),
                              disbursed=rng.choice(["", "2024-03-01", "2025-06-01"])))
        before = {ln.id: (ln.balance_cents, ln.arrears_cents) for ln in loans}
        excess = rng.choice([[ExcessBucket("deposits")],
                             [ExcessBucket("shares", percent=rng.randint(1, 99)), ExcessBucket("deposits")],
                             [ExcessBucket("deposits", max_cents=rng.randint(1, 90_000)), ExcessBucket("shares")]])
        rules = Rules(rng.choice(list(al.LOAN_ORDERS)), rng.sample(list(al.ARREARS_PARTS), 3), rng.random() < 0.7, excess)
        rules.validate()
        amount = rng.randint(1, 1_000_000)
        lines = al.plan(amount, loans, rules, rng.choice([None] + [ln.id for ln in loans]))
        assert sum(x.amount_cents for x in lines) == amount and all(x.amount_cents > 0 for x in lines)
        for ln in loans:
            paid = sum(x.amount_cents for x in lines if x.loan_id == ln.id)
            assert paid == before[ln.id][0] - ln.balance_cents <= before[ln.id][0]
            assert ln.arrears_cents >= 0 and ln.penalty_arrears_cents >= 0 and ln.interest_arrears_cents >= 0


@pytest.mark.parametrize("rules,msg", [
    (Rules(loan_order="random"), "loan_order"),
    (Rules(arrears_order=["penalty", "penalty", "principal"]), "each once"),
    (Rules(excess=[]), "at least one"),
    (Rules(excess=[ExcessBucket("deposits", percent=50)]), "last excess bucket"),
    (Rules(excess=[ExcessBucket("shares"), ExcessBucket("deposits")]), "either a percent or a cap"),
    (Rules(excess=[ExcessBucket("shares", percent=10), ExcessBucket("shares")]), "distinct"),
    (Rules(excess=[ExcessBucket("loans", percent=10), ExcessBucket("deposits")]), "distinct"),
])
def test_invalid_rules(rules, msg):
    with pytest.raises(ValueError, match=msg):
        rules.validate()


# ---------------------------------------------------------------- API and real payments

@pytest.fixture()
def member(env):
    c, Session = env
    with Session() as s:
        s.add(Member(id=10, institution_id=1, member_no="UT00104", name="Achieng Owino", phone="254711000001"))
        s.add(Member(id=20, institution_id=2, member_no="UT00999", name="Other SACCO member"))
        s.add(Loan(id=10, institution_id=1, member_id=10, loan_no="LN1", principal_cents=10_000_000,
                   balance_cents=5_000_000, installment_cents=500_000, arrears_cents=300_000,
                   penalty_arrears_cents=20_000, interest_arrears_cents=80_000, arrears_breakdown=True,
                   days_in_arrears=20,
                   disbursed_on=date(2025, 1, 1)))
        s.commit()
    return c, Session


RULES = {"loan_order": "most_overdue_first", "arrears_order": ["penalty", "interest", "principal"],
         "pay_current_installment": True, "excess": [{"target": "shares", "max_kes": 1000}, {"target": "deposits"}]}


def test_rules_api(member):
    c, Session = member
    viewer, admin = login(c, "viewer@a.test"), login(c, "admin@a.test")
    r = c.get("/institutions/1/allocation-rules", headers=viewer).json()
    assert r["is_default"] and r["excess"] == [{"target": "deposits", "percent": None, "max_kes": None}]
    assert c.put("/institutions/1/allocation-rules", headers=login(c, "accountant@a.test"), json=RULES).status_code == 403
    r = c.put("/institutions/1/allocation-rules", headers=admin, json=RULES)
    assert r.status_code == 200 and not r.json()["is_default"] and r.json()["excess"][0]["max_kes"] == 1000
    bad = {**RULES, "excess": [{"target": "deposits", "percent": 30}]}
    r = c.put("/institutions/1/allocation-rules", headers=admin, json=bad)
    assert r.status_code == 422 and "last excess bucket" in r.json()["detail"]
    assert c.put("/institutions/2/allocation-rules", headers=admin, json=RULES).status_code == 404
    with Session() as s:
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "allocation_rules.update"))
        assert ev.before["excess"][0]["target"] == "deposits" and ev.after["excess"][0]["target"] == "shares"


def test_api_keys_cannot_change_rules(member):
    c, _ = member
    key = c.post("/institutions/1/api-keys", headers=login(c, "admin@a.test"),
                 json={"name": "sync", "role": "accountant"}).json()["key"]
    assert c.put("/institutions/1/allocation-rules", headers={"Authorization": f"Bearer {key}"},
                 json=RULES).status_code == 403


def test_matching_uses_the_institutions_rules(member):
    c, Session = member
    c.put("/institutions/1/allocation-rules", headers=login(c, "admin@a.test"), json=RULES)
    with Session() as s:
        s.add(Transaction(institution_id=1, source="mpesa", reference="QX1", txn_time=datetime(2026, 9, 5),
                          amount_cents=1_000_000, account_ref="UT00104", payer_phone="254711000001"))
        s.commit()
        run_matching(s, 1)
        got = [(a.target, a.amount_cents) for a in s.scalars(select(Allocation).order_by(Allocation.id))]
        assert got == [("loan_penalty", 20_000), ("loan_interest", 80_000), ("loan_principal", 200_000),
                       ("loan_installment", 500_000), ("shares", 100_000), ("deposits", 100_000)]
        ln = s.get(Loan, 10)
        assert (ln.arrears_cents, ln.penalty_arrears_cents, ln.interest_arrears_cents) == (0, 0, 0)
    csv_text = c.get("/institutions/1/exports/postings.csv", headers=login(c, "accountant@a.test")).text
    assert ",loan_penalty,LN1,200.00" in csv_text and ",shares,,1000.00" in csv_text


def test_preview_changes_nothing(member):
    c, Session = member
    h = login(c, "viewer@a.test")
    draft = {**RULES, "arrears_order": ["principal", "interest", "penalty"]}
    r = c.post("/institutions/1/allocation-rules/preview", headers=h,
               json={"member_no": "UT00104", "amount_kes": "2500.50", "rules": draft})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lines"] == [{"target": "loan_principal", "loan_no": "LN1", "amount_kes": 2000.0},
                             {"target": "loan_interest", "loan_no": "LN1", "amount_kes": 500.5}]
    assert body["loans_before"][0]["arrears_kes"] == 3000 and body["loans_after"][0]["arrears_kes"] == 499.5
    with Session() as s:
        ln = s.get(Loan, 10)
        assert (ln.arrears_cents, ln.balance_cents) == (300_000, 5_000_000)
        assert s.scalar(select(Allocation)) is None
    current = c.post("/institutions/1/allocation-rules/preview", headers=h,
                     json={"member_no": "UT00104", "amount_kes": 100}).json()
    assert current["lines"][0]["target"] == "loan_penalty"  # current (default) rules
    assert c.post("/institutions/1/allocation-rules/preview", headers=h,
                  json={"member_no": "UT00999", "amount_kes": 100}).status_code == 404  # other SACCO's member


def test_loans_import_reads_penalty_and_interest(env):
    _, Session = env
    with Session() as s:
        s.add(Member(id=10, institution_id=1, member_no="UT00104", name="Achieng Owino"))
        s.commit()
        csv = ("Loan No,Member No,Principal,Balance,Installment,Arrears,Penalty Arrears,Interest Arrears,Days In Arrears\n"
               "LN1,UT00104,100000,50000,5000,3000,200,800,20\n"
               "LN2,UT00104,100000,50000,5000,3000,2500,800,20\n").encode()
        res = sources.import_loans(s, 1, csv)
        ok, bad = (s.scalar(select(Loan).where(Loan.loan_no == n)) for n in ("LN1", "LN2"))
        assert (ok.penalty_arrears_cents, ok.interest_arrears_cents, ok.arrears_cents) == (20_000, 80_000, 300_000)
        assert (bad.penalty_arrears_cents, bad.interest_arrears_cents, bad.arrears_cents) == (0, 0, 300_000)
        assert ok.arrears_breakdown and not bad.arrears_breakdown
        assert len(res.rejected) == 1 and "LN2" in res.rejected[0]
        sources.import_loans(s, 1, b"Loan No,Member No,Balance,Arrears\nLN3,UT00104,50000,3000\n")
        assert not s.scalar(select(Loan).where(Loan.loan_no == "LN3")).arrears_breakdown  # export without parts
