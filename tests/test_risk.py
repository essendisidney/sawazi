import pytest
from sqlalchemy import select

from sawazi.engine import exposure as ex
from sawazi.engine.exposure import LoanRow, MemberRow, Pledge
from sawazi.importers import sources
from sawazi.models import AuditEvent, CoreGuarantee, Loan, Member
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)
from tests.test_guarantor_lifecycle import disbursed_with_guarantor
from tests.test_guarantors import world  # noqa: F401  (world is a fixture)


def m(i, deposits=10_000_000, employer=None):
    return MemberRow(i, f"M{i}", f"Member {i}", deposits, employer)


def ln(i, member, balance=1_000_000, days=0, product="DEV"):
    return LoanRow(i, f"LN{i}", member, product, balance, balance // 10 if days else 0, days)


def kinds(r):
    return sorted((f.kind, tuple(f.member_nos)) for f in r.flags)


# ---------------------------------------------------------------- engine

@pytest.mark.parametrize("days,cls", [(0, "performing"), (1, "watch"), (30, "watch"), (31, "substandard"),
                                      (180, "substandard"), (181, "doubtful"), (360, "doubtful"), (361, "loss")])
def test_classification_boundaries(days, cls):
    assert ex.classify(days) == cls


def test_classification_and_provisions():
    loans = [ln(1, 1, 1_000_000, 0), ln(2, 2, 1_000_000, 10), ln(3, 3, 1_000_000, 100), ln(4, 4, 1_000_000, 400)]
    r = ex.report([m(i) for i in range(1, 5)], loans, [])
    got = {c["class"]: (c["loans"], c["provision_cents"]) for c in r.classification}
    assert got == {"performing": (1, 10_000), "watch": (1, 50_000), "substandard": (1, 250_000),
                   "doubtful": (0, 0), "loss": (1, 1_000_000)}
    assert r.portfolio["provision_cents"] == 1_310_000
    assert r.portfolio["par30_bps"] == 5000 and r.portfolio["par1_bps"] == 7500


def test_par_by_product_and_employer():
    members = [m(1, employer="Tumaini Schools"), m(2, employer="Tumaini Schools"), m(3)]
    loans = [ln(1, 1, 1_000_000, 45, "DEV"), ln(2, 2, 1_000_000, 0, "DEV"), ln(3, 3, 2_000_000, 0, "EMG")]
    r = ex.report(members, loans, [])
    assert r.par_by_product[0] == {"name": "DEV", "loans": 2, "balance_cents": 2_000_000, "at_risk_cents": 1_000_000,
                                   "par_bps": 5000}
    assert {row["name"]: row["par_bps"] for row in r.par_by_employer} == {"Tumaini Schools": 5000,
                                                                          "Not on check-off": 0}


def test_clean_book_raises_no_flags():
    members = [m(i) for i in range(1, 30)]
    loans = [ln(i, i, 1_000_000) for i in range(1, 30)]
    pledges = [Pledge(i + 1, i, i, 500_000, "core") for i in range(1, 29)]  # a chain, no loops, all current
    assert ex.report(members, loans, pledges).flags == []


def test_guarantor_flags():
    members = [m(1), m(2, deposits=1_000_000), m(3), m(4), m(5), m(6), m(7), m(8)]
    loans = [ln(1, 1), ln(2, 2, days=60), ln(3, 3), ln(4, 4), ln(5, 5), ln(6, 6), ln(7, 7, days=90), ln(8, 8)]
    pledges = [
        Pledge(2, 1, 1, 2_000_000, "core"),  # M2 is 60 days behind AND pledges above KES 10,000 deposits
        Pledge(3, 4, 4, 100_000, "sawazi"), Pledge(4, 3, 3, 100_000, "core"),  # M3 and M4 guarantee each other
        Pledge(5, 6, 6, 100_000, "core"), Pledge(6, 8, 8, 100_000, "core"), Pledge(8, 5, 5, 100_000, "core"),  # circle
        Pledge(2, 7, 7, 100_000, "core"),  # loan 7 is 90 days behind and its only guarantor (M2) is behind too
    ] + [Pledge(1, b, b, 10_000, "core") for b in (3, 4, 5, 6, 8)]  # M1 backs five loans
    got = kinds(ex.report(members, loans, pledges))
    assert ("guarantor_in_arrears", ("M2",)) in got
    assert ("over_pledged", ("M2",)) in got
    assert ("mutual_guarantee", ("M3", "M4")) in got
    assert ("guarantee_circle", ("M5", "M6", "M8")) in got
    assert ("chain_default", ("M7", "M2")) in got
    assert ("many_guarantees", ("M1",)) in got
    r = ex.report(members, loans, pledges)
    assert r.flags[0].severity == "high"  # worst first
    assert r.guarantors["on_loans_behind_cents"] == 100_000


def test_concentration():
    members = [m(i) for i in range(1, 25)]
    loans = [ln(1, 1, 9_000_000)] + [ln(i, i, 1_000_000) for i in range(2, 25)]  # M1 holds 9 of 32 million
    r = ex.report(members, loans, [])
    assert kinds(r) == [("concentration", ("M1",))]
    assert r.concentration["top"][0]["share_bps"] == 2812
    small = ex.report(members[:2], loans[:2], [])  # two loans: every borrower is "large"; that is not a warning
    assert small.flags == [] and small.concentration["top"][0]["share_bps"] == 9000


# ---------------------------------------------------------------- core guarantees

CSV = b"Loan No,Guarantor,Amount Guaranteed\n"


@pytest.fixture()
def book(env):
    c, Session = env
    with Session() as s:
        s.add_all([Member(id=10 + i, institution_id=1, member_no=f"M{i}", name=f"Member {i}", deposits_cents=5_000_000)
                   for i in range(1, 5)])
        s.add(Member(id=99, institution_id=2, member_no="M1", name="Other SACCO"))
        s.flush()
        s.add_all([Loan(id=10 + i, institution_id=1, member_id=10 + i, loan_no=f"L{i}", principal_cents=2_000_000,
                        balance_cents=1_000_000, installment_cents=1, arrears_cents=0, days_in_arrears=0)
                   for i in range(1, 4)])
        s.commit()
    return c, Session


def upload(c, body, replace=False, who="accountant@a.test", iid=1):
    r = c.post(f"/institutions/{iid}/import/core_guarantees", headers=login(c, who), params={"replace": replace},
               files={"file": ("g.csv", CSV + body)})
    return r


def test_core_guarantee_import(book):
    c, Session = book
    r = upload(c, b"L1,M2,10000\nL1,M2,5000\nL2,M2,0\nL3,M3,1000\nNOPE,M1,1\nL1,ZZZ,1\nL2,M1,20000\n").json()
    assert r["created"] == 2 and r["rejected_count"] == 5
    assert any("appears twice" in x for x in r["rejected"]) and any("own loan" in x for x in r["rejected"])
    exp = c.get("/institutions/1/members/M2/guarantor-exposure", headers=login(c, "viewer@a.test")).json()
    assert exp["pledged_kes"] == 10_000 and exp["free_kes"] == 40_000 and exp["guarantees"][0]["source"] == "core"

    r = upload(c, b"L1,M2,12000\nL2,M1,20000\n").json()
    assert r["created"] == 0 and r["updated"] == 1  # re-upload updates, never duplicates
    upload(c, b"L1,M2,12000\n")  # a partial file releases nothing
    with Session() as s:
        assert sorted(s.scalars(select(CoreGuarantee.status))) == ["active", "active"]
    r = upload(c, b"L1,M2,12000\n", replace=True).json()
    assert r["released"] == 1  # a complete list releases what is no longer in it
    assert upload(c, b"L1,M2,1\n", who="viewer@a.test").status_code == 403
    assert upload(c, b"L1,M2,1\n", iid=2).status_code == 404


def test_core_guarantees_released_when_the_loan_is_repaid(book):
    c, Session = book
    upload(c, b"L1,M2,10000\n")
    r = c.post("/institutions/1/import/loans", headers=login(c, "accountant@a.test"),
               files={"file": ("l.csv", b"Loan No,Member No,Principal,Balance,Installment,Arrears\nL1,M1,20000,0,1,0\n")})
    assert r.json()["guarantees_released"] == 1
    with Session() as s:
        assert s.scalar(select(CoreGuarantee.status)) == "released"
        assert s.scalar(select(AuditEvent.action).where(AuditEvent.action == "core_guarantee.release"))


def test_a_pledge_known_to_both_systems_counts_once(world):
    """After disbursement the core system records Sawazi's guarantor too: it must not be counted twice."""
    c, Session, phone, app_id, _, officer = world
    disbursed_with_guarantor(c, Session, phone, app_id, officer)  # G1 guarantees KES 200,000 of LN900 in Sawazi
    r = c.post("/institutions/1/import/core_guarantees", headers=login(c, "accountant@a.test"),
               files={"file": ("g.csv", CSV + b"LN900,G1,200000\n")})
    assert r.json()["created"] == 1
    exp = c.get("/institutions/1/members/G1/guarantor-exposure", headers=officer).json()
    assert exp["pledged_kes"] == 200_000 and exp["free_kes"] == 100_000
    risk = c.get("/institutions/1/risk", headers=officer).json()
    assert risk["guarantors"]["pledged_kes"] == 200_000


# ---------------------------------------------------------------- the risk endpoint

def test_risk_endpoint(book):
    c, Session = book
    with Session() as s:
        s.get(Loan, 12).days_in_arrears = 45
        s.commit()
    upload(c, b"L1,M2,10000\nL2,M1,10000\nL3,M2,5000\n")
    r = c.get("/institutions/1/risk", headers=login(c, "viewer@a.test"))
    assert r.status_code == 200
    body = r.json()
    assert body["portfolio"]["loans"] == 3 and body["portfolio"]["par30_pct"] == 33.33
    assert {x["class"]: x["loans"] for x in body["classification"]}["substandard"] == 1
    flagged = {(f["kind"], tuple(f["member_nos"])) for f in body["flags"]}
    assert ("mutual_guarantee", ("M1", "M2")) in flagged  # M2 backs L1 (M1), M1 backs L2 (M2)
    assert ("guarantor_in_arrears", ("M2",)) in flagged  # M2's own loan L2 is 45 days behind
    assert c.get("/institutions/2/risk", headers=login(c, "admin@a.test")).status_code == 404
