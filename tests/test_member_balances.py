from datetime import date

from sqlalchemy import select

from sawazi.importers import sources
from sawazi.models import Member
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


def member(s, no):
    return s.scalar(select(Member).where(Member.member_no == no))


def test_members_export_with_figures(env):
    _, Session = env
    csv = ("Member No,Name,Date Joined,Deposits,Share Capital,Gross Pay,Net Pay,Balances As At\n"
           "M1,Achieng Owino,15/03/2021,\"120,000.00\",\"10,000.00\",\"90,000\",\"40,000\",31/08/2026\n"
           "M2,Kamau Njoroge,02/09/2026,0.00,,,,31/08/2026\n").encode()
    with Session() as s:
        res = sources.import_members(s, 1, csv)
        a, b = member(s, "M1"), member(s, "M2")
        assert res.created == 2 and not res.rejected
        assert (a.joined_on, a.deposits_cents, a.shares_cents) == (date(2021, 3, 15), 12_000_000, 1_000_000)
        assert (a.gross_pay_cents, a.net_pay_cents, a.balances_as_of, a.pay_as_of) == (
            9_000_000, 4_000_000, date(2026, 8, 31), date(2026, 8, 31))
        assert b.deposits_cents == 0  # zero is a real figure
        assert b.shares_cents is None and b.gross_pay_cents is None  # blank means "not supplied", never zero


def test_members_export_without_figures_keeps_them(env):
    _, Session = env
    with Session() as s:
        sources.import_members(s, 1, b"Member No,Name,Deposits\nM1,A,50000\n")
        sources.import_members(s, 1, b"Member No,Name,Phone\nM1,A Renamed,0711000001\n")
        m = member(s, "M1")
        assert m.name == "A Renamed" and m.deposits_cents == 5_000_000


def test_balances_import_updates_only_what_the_file_has(env):
    _, Session = env
    with Session() as s:
        sources.import_members(s, 1, b"Member No,Name,Deposits,Share Capital,Gross Pay,Net Pay\nM1,A,50000,5000,80000,30000\n")
        res = sources.import_member_balances(s, 1, b"Member No,Deposits,As At\nM1,62500.50,30/09/2026\nNOPE,1\n")
        m = member(s, "M1")
        assert res.updated == 1 and res.created == 0 and res.as_dict()["updated"] == 1
        assert res.rejected == ["row 3: unknown member 'NOPE'"]
        assert (m.deposits_cents, m.shares_cents, m.balances_as_of) == (6_250_050, 500_000, date(2026, 9, 30))
        assert (m.gross_pay_cents, m.net_pay_cents) == (8_000_000, 3_000_000)  # untouched: not in this file
        assert s.scalar(select(Member).where(Member.member_no == "NOPE")) is None  # never creates members


def test_payroll_file_and_bad_figures(env):
    _, Session = env
    with Session() as s:
        sources.import_members(s, 1, b"Member No,Name\nM1,A\nM2,B\nM3,C\n")
        res = sources.import_member_balances(s, 1, (
            "Member No,Gross Pay,Net Pay,Pay Date,Deposits,Date Joined\n"
            "M1,100000,35000,25/09/2026,,\n"
            "M2,50000,60000,25/09/2026,,\n"       # net more than gross: cannot be right
            "M3,,,,-500,32/13/2020\n").encode())  # negative deposits, impossible date
        a, b, c = member(s, "M1"), member(s, "M2"), member(s, "M3")
        assert (a.gross_pay_cents, a.net_pay_cents, a.pay_as_of) == (10_000_000, 3_500_000, date(2026, 9, 25))
        assert a.balances_as_of is None  # a payroll file says nothing about balances
        assert b.gross_pay_cents is None and b.net_pay_cents is None
        assert c.deposits_cents is None and c.joined_on is None
        assert len(res.rejected) == 3 and "net pay is more than gross" in res.rejected[0]


def test_blank_cell_never_wipes_a_known_figure(env):
    _, Session = env
    with Session() as s:
        sources.import_members(s, 1, b"Member No,Name,Deposits\nM1,A,50000\n")
        sources.import_member_balances(s, 1, b"Member No,Deposits,Gross Pay,Net Pay\nM1,,90000,40000\n")
        assert member(s, "M1").deposits_cents == 5_000_000


def test_balances_upload_through_the_api(env):
    c, Session = env
    with Session() as s:
        sources.import_members(s, 1, b"Member No,Name\nM1,A\n")
        sources.import_members(s, 2, b"Member No,Name\nM1,Other SACCO\n")
    h = login(c, "accountant@a.test")
    r = c.post("/institutions/1/import/member_balances", headers=h,
               files={"file": ("bal.csv", b"Member No,Deposits\nM1,70000\n")})
    assert r.status_code == 200 and r.json()["updated"] == 1
    with Session() as s:
        got = {m.institution_id: m.deposits_cents for m in s.scalars(select(Member).where(Member.member_no == "M1"))}
        assert got == {1: 7_000_000, 2: None}  # the other institution's M1 is untouched
    assert c.post("/institutions/1/import/member_balances", headers=login(c, "viewer@a.test"),
                  files={"file": ("bal.csv", b"Member No,Deposits\nM1,1\n")}).status_code == 403
