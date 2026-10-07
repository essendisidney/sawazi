import csv
import io
from datetime import date

import pytest
from sqlalchemy import select

from sawazi import auth
from sawazi.importers import sources
from sawazi.models import AuditEvent, LoanApplication, Member, StaffUser
from tests.test_auth import PW, env, login  # noqa: F401  (env is a fixture)

PRODUCT = {"code": "DEV", "name": "Development Loan", "max_amount_kes": 2_000_000, "max_term_months": 48,
           "interest_rate_pct": 12, "second_approval_above_kes": 500_000}


@pytest.fixture()
def world(env):
    c, Session = env
    with Session() as s:
        s.add(Member(id=10, institution_id=1, member_no="M1", name="Achieng Owino", joined_on=date(2020, 1, 1),
                     deposits_cents=70_000_000, gross_pay_cents=15_000_000, net_pay_cents=9_000_000))
        s.add(Member(id=11, institution_id=1, member_no="M2", name="New Member", joined_on=date(2026, 9, 1),
                     deposits_cents=1_000_000))
        s.add(StaffUser(institution_id=1, email="approver2@a.test", name="Second Approver", role="approver",
                        password_hash=auth.hash_password(PW), is_active=True))
        s.commit()
    pid = c.post("/institutions/1/loan-products", headers=login(c, "admin@a.test"), json=PRODUCT).json()["id"]
    return c, Session, pid


def apply(c, pid, who="credit_officer@a.test", member="M1", amount=200_000, term=24):
    r = c.post("/institutions/1/loan-applications", headers=login(c, who),
               json={"member_no": member, "product_id": pid, "amount_kes": amount, "term_months": term,
                     "purpose": "Dairy cows"})
    assert r.status_code == 200, r.text
    return r.json()


def submit(c, app_id, who="credit_officer@a.test"):
    r = c.post(f"/institutions/1/loan-applications/{app_id}/submit", headers=login(c, who))
    assert r.status_code == 200, r.text
    return r.json()


def decide(c, app_id, who="approver@a.test", **body):
    return c.post(f"/institutions/1/loan-applications/{app_id}/decide", headers=login(c, who),
                  json={"decision": "approve", **body})


def test_from_draft_to_disbursed(world):
    c, Session, pid = world
    a = apply(c, pid)
    assert a["status"] == "draft" and a["appraisal"]["outcome"] == "passes"  # live appraisal on drafts
    r = c.patch(f"/institutions/1/loan-applications/{a['id']}", headers=login(c, "credit_officer@a.test"),
                json={"amount_kes": 250_000})
    assert r.json()["amount_kes"] == 250_000
    a = submit(c, a["id"])
    assert a["status"] == "submitted" and a["approvals_needed"] == 1 and a["appraisal_outcome"] == "passes"
    a = decide(c, a["id"], note="Good record").json()
    assert a["status"] == "approved" and a["decisions"][0]["decision"] == "approve"

    acc = login(c, "accountant@a.test")
    rows = list(csv.DictReader(io.StringIO(c.post("/institutions/1/loan-applications/export.csv", headers=acc).text)))
    assert [(r["sawazi_ref"], r["member_no"], r["amount"], r["approved_by"], r["with_exceptions"]) for r in rows] == [
        (f"SWZ-{a['id']}", "M1", "250000.00", "approver a", "no")]
    again = c.post("/institutions/1/loan-applications/export.csv", headers=acc).text
    assert again.count("\n") == 1  # header only: each loan is handed over exactly once

    with Session() as s:  # the core system disburses; it shows up in the next loans export
        sources.import_loans(s, 1, b"Loan No,Member No,Principal,Balance,Installment,Arrears\n"
                                   b"LN900,M1,250000,250000,11768,0\n")
    a = c.get(f"/institutions/1/loan-applications/{a['id']}", headers=acc).json()
    assert a["status"] == "disbursed" and a["disbursed_at"]
    with Session() as s:
        actions = [e.action for e in s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "loan_application")
                                               .order_by(AuditEvent.id))]
    assert actions == ["loan_application.create", "loan_application.update", "loan_application.submit",
                       "loan_application.approve"]


def test_maker_checker(world):
    c, _, pid = world
    a = submit(c, apply(c, pid, who="approver@a.test")["id"], who="approver@a.test")
    r = decide(c, a["id"], who="approver@a.test")
    assert r.status_code == 403 and "another approver" in r.json()["detail"]
    assert decide(c, a["id"], who="approver2@a.test").json()["status"] == "approved"


def test_two_approvers_above_the_threshold(world):
    c, _, pid = world
    a = submit(c, apply(c, pid, amount=600_000, term=48)["id"])
    assert a["approvals_needed"] == 2
    assert decide(c, a["id"]).json()["status"] == "submitted"  # one of two
    assert decide(c, a["id"]).status_code == 409  # the same approver cannot count twice
    assert decide(c, a["id"], who="approver2@a.test").json()["status"] == "approved"


def test_one_decline_ends_it(world):
    c, _, pid = world
    a = submit(c, apply(c, pid, amount=600_000, term=48)["id"])
    decide(c, a["id"])
    r = decide(c, a["id"], who="approver2@a.test", decision="decline", note="Too much exposure")
    assert r.json()["status"] == "declined"
    assert decide(c, a["id"], who="approver2@a.test").status_code == 409


def test_failed_checks_need_a_written_override(world):
    c, _, pid = world
    a = submit(c, apply(c, pid, member="M2", amount=50_000, term=12)["id"])  # 1 month a member, no payslip
    assert a["appraisal_outcome"] == "fails"
    r = decide(c, a["id"])
    assert r.status_code == 422 and "override_reason" in r.json()["detail"]
    assert decide(c, a["id"], override_reason="short").status_code == 422
    a = decide(c, a["id"], override_reason="Transferred from Tumaini SACCO with 8 years' history").json()
    assert a["status"] == "approved" and "Tumaini" in a["override_reason"]
    rows = c.post("/institutions/1/loan-applications/export.csv", headers=login(c, "accountant@a.test")).text
    assert rows.strip().endswith(",yes")


@pytest.mark.parametrize("who", ["viewer@a.test", "accountant@a.test"])
def test_who_may_apply(world, who):
    c, _, pid = world
    r = c.post("/institutions/1/loan-applications", headers=login(c, who),
               json={"member_no": "M1", "product_id": pid, "amount_kes": 1000, "term_months": 1})
    assert r.status_code == 403


@pytest.mark.parametrize("who", ["admin@a.test", "credit_officer@a.test", "accountant@a.test", "viewer@a.test"])
def test_only_approvers_decide(world, who):
    c, _, pid = world
    a = submit(c, apply(c, pid)["id"])
    assert decide(c, a["id"], who=who).status_code == 403


def test_only_drafts_change_and_withdrawal(world):
    c, _, pid = world
    a = submit(c, apply(c, pid)["id"])
    h = login(c, "credit_officer@a.test")
    assert c.patch(f"/institutions/1/loan-applications/{a['id']}", headers=h, json={"term_months": 12}).status_code == 409
    assert c.post(f"/institutions/1/loan-applications/{a['id']}/submit", headers=h).status_code == 409
    r = c.post(f"/institutions/1/loan-applications/{a['id']}/withdraw", headers=h, params={"note": "member changed mind"})
    assert r.json()["status"] == "withdrawn"
    assert decide(c, a["id"]).status_code == 409
    approved = decide(c, submit(c, apply(c, pid)["id"])["id"]).json()
    assert c.post(f"/institutions/1/loan-applications/{approved['id']}/withdraw", headers=h,
                  params={"note": "x y z"}).status_code == 409


def test_disbursement_is_never_guessed(world):
    c, Session, pid = world
    for _ in range(2):  # two approved loans for the same member and amount
        decide(c, submit(c, apply(c, pid)["id"])["id"])
    c.post("/institutions/1/loan-applications/export.csv", headers=login(c, "accountant@a.test"))
    with Session() as s:
        sources.import_loans(s, 1, b"Loan No,Member No,Principal,Balance,Installment,Arrears\n"
                                   b"LN901,M1,200000,200000,9415,0\n")
        assert {a.status for a in s.scalars(select(LoanApplication))} == {"exported"}  # which one? not ours to guess


def test_applications_stay_in_their_institution(world):
    c, _, pid = world
    a = apply(c, pid)
    b_admin = login(c, "admin@b.test")
    assert c.get(f"/institutions/1/loan-applications/{a['id']}", headers=b_admin).status_code == 404
    assert c.get(f"/institutions/2/loan-applications/{a['id']}", headers=b_admin).status_code == 404
    assert c.post("/institutions/1/loan-applications", headers=login(c, "credit_officer@b.test"),
                  json={"member_no": "M1", "product_id": pid, "amount_kes": 1000, "term_months": 1}).status_code == 404
    assert c.get("/institutions/2/loan-applications", headers=b_admin).json() == []
