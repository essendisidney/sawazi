from datetime import date

from sqlalchemy import select

from sawazi import auth, boardpack
from sawazi.models import AuditEvent, PortfolioSnapshot
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)
from tests.test_guarantors import world  # noqa: F401  (world is a fixture)

THIS_MONTH = auth.utcnow().strftime("%Y-%m")


def pack(c, who="admin@a.test", month=THIS_MONTH):
    return c.get("/institutions/1/board-pack.html", headers=login(c, who), params={"month": month})


def test_snapshots_after_loans_upload_and_on_demand(world):
    c, Session, *_ = world
    acc = login(c, "accountant@a.test")
    c.post("/institutions/1/import/loans", headers=acc, files={"file": ("l.csv", b"Loan No,Member No,Principal,Balance,"
                                                                              b"Installment,Arrears,Days In Arrears\n"
                                                                              b"LN1,M1,100000,80000,5000,10000,45\n")})
    snaps = c.get("/institutions/1/snapshots", headers=login(c, "viewer@a.test")).json()
    assert len(snaps) == 1 and snaps[0]["par30_pct"] == 100 and snaps[0]["loans"] == 2  # with the fixture's loan
    assert c.post("/institutions/1/snapshots", headers=acc).status_code == 200
    assert len(c.get("/institutions/1/snapshots", headers=acc).json()) == 1  # same day: replaced, not added
    for who in ("viewer@a.test", "credit_officer@a.test"):
        assert c.post("/institutions/1/snapshots", headers=login(c, who)).status_code == 403
        assert pack(c, who).status_code == 403


def test_board_pack_this_month(world):
    c, Session, phone, app_id, pid, officer = world
    # a loan approved despite a failing appraisal, a rule change, and a failed login: all belong in the pack
    c.post(f"/institutions/1/loan-applications/{app_id}/submit", headers=officer)
    r = c.post(f"/institutions/1/loan-applications/{app_id}/decide", headers=login(c, "approver@a.test"),
               json={"decision": "approve", "override_reason": "Long record <script>alert(1)</script> with us"})
    assert r.json()["status"] == "approved", r.text
    c.put("/institutions/1/allocation-rules", headers=login(c, "admin@a.test"),
          json={"loan_order": "oldest_loan_first", "arrears_order": ["penalty", "interest", "principal"],
                "pay_current_installment": True, "excess": [{"target": "deposits"}]})
    c.post("/auth/login", json={"email": "admin@a.test", "password": "wrong-password"})

    r = pack(c, "approver@a.test")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["content-disposition"] == f"attachment; filename=sawazi_board_pack_{THIS_MONTH}.html"
    doc = r.text
    for heading in ("1. Portfolio quality", "2. Collections and payments", "3. Lending", "4. Guarantor exposure",
                    "5. Governance"):
        assert heading in doc
    assert "<script" not in doc.lower()  # the override reason and the member's name are text, never markup
    assert "&lt;script&gt;" in doc and "&lt;b&gt;Owino&lt;/b&gt;" in doc
    assert "Approved with exceptions (1)" in doc and "SWZ-" in doc and "approver a" in doc
    assert "Allocation rules changed" in doc and "which loan first: most overdue first → oldest loan first" in doc
    assert "(to date)" in doc and "In brief" in doc
    assert "1 approved with exceptions." in doc  # the headline says so before any table
    assert "Failed logins against staff accounts: 1." in doc
    with Session() as s:
        assert s.scalar(select(AuditEvent.note).where(AuditEvent.action == "board_pack.generate")) == \
            f"board pack {THIS_MONTH}"


def test_a_month_without_a_snapshot_says_so(world):
    c, *_ = world
    doc = pack(c, month="2025-01").text
    assert "No portfolio snapshot was taken this month" in doc
    assert pack(c, month="2999-01").status_code == 422
    assert pack(c, month="2026-13").status_code == 422


def test_trend_and_comparison_come_from_snapshots(world):
    c, Session, *_ = world
    with Session() as s:
        for as_of, par30, bal in [(date(2026, 7, 31), 800, 50_000_000), (date(2026, 8, 31), 650, 52_000_000),
                                  (date(2026, 9, 30), 500, 55_000_000)]:
            fig = boardpack.snapshot_figures(s, 1)
            fig["portfolio"] |= {"par30_bps": par30, "par1_bps": par30 * 2, "balance_cents": bal}
            s.add(PortfolioSnapshot(institution_id=1, as_of=as_of, taken_at=auth.utcnow(), figures=fig))
        s.commit()
    doc = pack(c, month="2026-09").text
    assert "Portfolio figures as at 30 September 2026, compared with 31 August 2026." in doc
    assert "<polyline" in doc and "PAR 30 5.0%" in doc
    assert "▼ 1.5%" in doc  # PAR 30 fell from 6.5% to 5.0%: shown as an improvement


def test_snapshots_carry_the_date_their_figures_are_as_at(world):
    """A month-end export uploaded days later must count for the month it describes."""
    c, Session, *_ = world
    acc = login(c, "accountant@a.test")
    r = c.post("/institutions/1/import/loans", headers=acc, params={"as_of": "2026-09-30"},
               files={"file": ("l.csv", b"Loan No,Member No,Principal,Balance,Installment,Arrears\nLN1,M1,100000,80000,5000,0\n")})
    assert r.json()["snapshot_as_of"] == "2026-09-30"
    assert c.post("/institutions/1/snapshots", headers=acc, params={"as_of": "2026-08-31"}).json()["as_of"] == "2026-08-31"
    assert [x["as_of"] for x in c.get("/institutions/1/snapshots", headers=acc).json()] == ["2026-09-30", "2026-08-31"]
    assert "compared with 31 August 2026" in pack(c, "accountant@a.test", "2026-09").text
    assert c.post("/institutions/1/snapshots", headers=acc, params={"as_of": "2999-01-01"}).status_code == 422
    r = c.post("/institutions/1/import/loans", headers=acc, params={"as_of": "2999-01-01"},
               files={"file": ("l.csv", b"Loan No,Member No,Principal,Balance,Installment,Arrears\nLN1,M1,1,1,1,0\n")})
    assert r.status_code == 422
    with Session() as s:  # refused means nothing was imported
        from sawazi.models import Loan
        assert s.scalar(select(Loan.balance_cents).where(Loan.loan_no == "LN1")) == 8_000_000
