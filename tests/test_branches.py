from datetime import date

import pytest
from sqlalchemy import select

from sawazi import boardpack
from sawazi.models import AuditEvent, Institution, Loan, Member, PortfolioSnapshot, Reminder
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


def test_par_by_branch_uses_the_loans_branch_then_the_members(env):
    c, Session = env
    with Session() as s:
        s.add_all([Member(id=1, institution_id=1, member_no="M1", name="A", branch="Kisumu"),
                   Member(id=2, institution_id=1, member_no="M2", name="B", branch="Kisumu")])
        s.flush()
        s.add_all([Loan(id=1, institution_id=1, member_id=1, loan_no="L1", principal_cents=1, balance_cents=100_00,
                        installment_cents=1, arrears_cents=50_00, days_in_arrears=45, branch="Mombasa"),
                   Loan(id=2, institution_id=1, member_id=2, loan_no="L2", principal_cents=1, balance_cents=300_00,
                        installment_cents=1)])
        s.commit()
    r = c.get("/institutions/1/risk", headers=login(c, "viewer@a.test")).json()
    rows = {b["name"]: b for b in r["par_by_branch"]}
    assert rows["Mombasa"]["par_pct"] == 100.0 and rows["Kisumu"]["par_pct"] == 0.0
    up = c.post("/institutions/1/import/loans", headers=login(c, "admin@a.test"), files={"file": ("l.csv", (
        "Loan No,Member No,Balance,Arrears,Installment,Days In Arrears,Branch\nL2,M2,300,0,1,0,Nakuru\n").encode())})
    assert up.status_code == 200
    with Session() as s:
        assert s.get(Loan, 2).branch == "Nakuru"
        assert "Weakest branch: Mombasa" in boardpack.render(s.get(Institution, 1), boardpack.gather(
            s, 1, date.today().strftime("%Y-%m")), "test")


def test_benchmarks_need_five_network_saccos_and_show_only_quartiles(env):
    c, Session = env
    viewer = login(c, "viewer@a.test")
    with Session() as s:
        s.get(Institution, 1).network_opt_in = True
        for iid in range(3, 8):
            if iid < 7:
                s.add(Institution(id=iid, name=f"Peer {iid}", network_opt_in=True))
        s.flush()
        for iid, par30 in ((1, 800), (2, 9999), (3, 200), (4, 400), (5, 600)):
            s.add(PortfolioSnapshot(institution_id=iid, as_of=date(2026, 9, 30), taken_at=date(2026, 9, 30),
                                    figures={"portfolio": {"loans": 10, "par1_bps": par30, "par30_bps": par30,
                                                           "par90_bps": 0}}))
        s.commit()
    b = c.get("/institutions/1/network/benchmarks", headers=viewer).json()
    assert b["ready"] is False and b["saccos"] == 4  # institution 2 has not joined; 4 is too few
    with Session() as s:
        s.add(PortfolioSnapshot(institution_id=6, as_of=date(2026, 9, 30), taken_at=date(2026, 9, 30),
                                figures={"portfolio": {"loans": 5, "par1_bps": 1000, "par30_bps": 1000, "par90_bps": 0}}))
        s.commit()
    b = c.get("/institutions/1/network/benchmarks", headers=viewer).json()
    par30 = next(m for m in b["measures"] if m["key"] == "par30_bps")
    assert b["saccos"] == 5 and (par30["own_pct"], par30["median_pct"], par30["p25_pct"], par30["p75_pct"]) == (8.0, 6.0, 4.0, 8.0)
    assert set(b) == {"opted_in", "ready", "saccos", "measures"}  # no SACCO names, no per-SACCO figures


def test_nightly_job_needs_its_secret(env, monkeypatch):
    c, Session = env
    monkeypatch.delenv("CRON_SECRET", raising=False)
    monkeypatch.delenv("SAWAZI_CRON_SECRET", raising=False)
    assert c.get("/cron/daily").status_code == 503  # never open
    monkeypatch.setenv("CRON_SECRET", "test-cron-secret-value")
    assert c.get("/cron/daily", headers={"Authorization": "Bearer wrong"}).status_code == 401
    with Session() as s:
        s.add(Member(id=1, institution_id=1, member_no="M1", name="A"))
        s.flush()
        s.add(Loan(id=1, institution_id=1, member_id=1, loan_no="L1", principal_cents=1, balance_cents=100_00,
                   installment_cents=10_00, arrears_cents=10_00, days_in_arrears=5))
        s.commit()
    r = c.get("/cron/daily", headers={"Authorization": "Bearer test-cron-secret-value"})
    assert r.status_code == 200 and r.json()["queued"]["1"] == 1
    with Session() as s:
        assert s.scalar(select(Reminder.loan_id)) == 1
        assert s.query(AuditEvent).filter(AuditEvent.actor_name == "Nightly job").count() == 2  # every institution


@pytest.mark.parametrize("path", ["/institutions/1/network/benchmarks"])
def test_benchmarks_need_sign_in(env, path):
    c, _ = env
    assert c.get(path).status_code == 401
