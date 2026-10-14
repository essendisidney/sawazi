from datetime import date, timedelta

from sqlalchemy import select

from sawazi import auth, boardpack
from sawazi.models import LendingGroup, Loan, Member, PromiseToPay, StaffUser
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)
from tests.test_member_app import bearer, members, sign_up  # noqa: F401  (members is a fixture)


def test_my_day_shows_only_what_is_mine(env):
    c, Session = env
    today = auth.utcnow().date()
    with Session() as s:
        officer = s.scalar(select(StaffUser).where(StaffUser.email == "credit_officer@a.test"))
        s.add(Member(id=1, institution_id=1, member_no="M1", name="Achieng Owino", phone="254711000001"))
        s.flush()
        s.add_all([Loan(id=1, institution_id=1, member_id=1, loan_no="L1", principal_cents=1, balance_cents=1,
                        installment_cents=1, arrears_cents=2_000_00, days_in_arrears=4, officer=officer.name.upper()),
                   Loan(id=2, institution_id=1, member_id=1, loan_no="L2", principal_cents=1, balance_cents=1,
                        installment_cents=1, arrears_cents=2_000_00, days_in_arrears=4, officer="Someone Else")])
        s.add(LendingGroup(institution_id=1, group_no="UMJ01", name="Umoja", meeting_day=today.weekday(),
                           frequency="weekly", officer_user_id=officer.id, status="active", created_at=auth.utcnow()))
        s.flush()
        s.add(PromiseToPay(institution_id=1, loan_id=2, amount_cents=1_000_00, made_on=today - timedelta(days=5),
                           due_on=today, recorded_by_user_id=officer.id, created_at=auth.utcnow()))
        s.commit()
    d = c.get("/institutions/1/my-day", headers=login(c, "credit_officer@a.test")).json()
    assert [x["loan_no"] for x in d["newly_behind"]] == ["L1"]  # officer name matched regardless of case
    assert [x["loan_no"] for x in d["promises_due"]] == ["L2"]  # recorded it, so it is theirs
    assert [g["group_no"] for g in d["groups_today"]] == ["UMJ01"]
    other = c.get("/institutions/1/my-day", headers=login(c, "viewer@a.test")).json()
    assert other["newly_behind"] == other["promises_due"] == other["groups_today"] == []


def test_data_quality_counts_blanks(env):
    c, Session = env
    with Session() as s:
        s.add_all([Member(id=1, institution_id=1, member_no="M1", name="A", phone="254711000001", employer="County"),
                   Member(id=2, institution_id=1, member_no="M2", name="B")])
        s.commit()
    q = {f["field"]: f for f in c.get("/institutions/1/data-quality", headers=login(c, "viewer@a.test")).json()["fields"]}
    assert (q["Phone"]["missing"], q["Phone"]["complete_pct"]) == (1, 50.0)
    assert (q["Net pay"]["of"], q["Net pay"]["missing"]) == (1, 1)  # only employed members need a payslip


def test_member_promises_from_the_app_and_board_pack_flow(members):
    c, Session, phone = members
    today = auth.utcnow().date()
    with Session() as s:
        s.add(Loan(id=70, institution_id=1, member_id=10, loan_no="L70", principal_cents=1, balance_cents=10_000_00,
                   installment_cents=1_000_00, arrears_cents=3_000_00, days_in_arrears=12))
        s.commit()
    tok = bearer(sign_up(c, phone)["session_token"])
    far = c.post("/m/promises", headers=tok, json={"loan_no": "L70", "amount_kes": 3000,
                                                    "due_on": str(today + timedelta(days=90))})
    assert far.status_code == 422 and "60 days" in far.json()["detail"]
    sw = c.post("/m/promises", headers=tok | {"Accept-Language": "sw"},
                json={"loan_no": "L99", "amount_kes": 1, "due_on": str(today)})
    assert sw.status_code == 422
    ok = c.post("/m/promises", headers=tok, json={"loan_no": "L70", "amount_kes": 3000, "due_on": str(today + timedelta(days=5))})
    assert ok.status_code == 200
    assert c.post("/m/promises", headers=tok, json={"loan_no": "L70", "amount_kes": 1, "due_on": str(today)}).status_code == 409
    loan = c.get("/m/overview", headers=tok).json()["loans"][0]
    assert loan["promise"]["state"] == "open" and loan["promise"]["kes"] == 3000
    staff = c.get("/institutions/1/promises", headers=login(c, "viewer@a.test")).json()
    assert staff[0]["note"] == "from the member app"

    with Session() as s:
        boardpack.take_snapshot(s, 1, date(2026, 8, 31))
        s.get(Loan, 70).days_in_arrears = 0
        boardpack.take_snapshot(s, 1, date(2026, 9, 30))
        s.commit()
        snap = boardpack.latest_in(s, 1, "2026-09")
        assert snap.figures["flow"]["behind_1_30"] == 1 and snap.figures["flow"]["cured"] == 1
