from datetime import date, datetime, timedelta

from sqlalchemy import select

from sawazi import auth, boardpack
from sawazi.engine import appraisal
from sawazi.models import Allocation, Loan, Member, Reminder, Transaction
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


def _book(Session):
    with Session() as s:
        s.add_all([Member(id=1, institution_id=1, member_no="M1", name="Achieng Owino", insider="board"),
                   Member(id=2, institution_id=1, member_no="M2", name="Otieno Kamau")])
        s.flush()
        s.add_all([Loan(id=1, institution_id=1, member_id=1, loan_no="L1", principal_cents=1, balance_cents=50_000_00,
                        installment_cents=5_000_00, arrears_cents=5_000_00, days_in_arrears=40, officer="Kamau"),
                   Loan(id=2, institution_id=1, member_id=2, loan_no="L2", principal_cents=1, balance_cents=30_000_00,
                        installment_cents=3_000_00, arrears_cents=3_000_00, days_in_arrears=10)])
        s.commit()


def _pay(Session, loan_id, cents, when):
    with Session() as s:
        t = Transaction(institution_id=1, source="mpesa", reference=f"R{loan_id}{cents}", amount_cents=cents,
                        txn_time=when, status="allocated", member_id=loan_id)
        s.add(t)
        s.flush()
        s.add(Allocation(institution_id=1, transaction_id=t.id, target="loan", loan_id=loan_id, amount_cents=cents))
        s.commit()


def test_promises_are_judged_by_payments_and_hold_the_queue(env):
    c, Session = env
    _book(Session)
    officer, viewer = login(c, "credit_officer@a.test"), login(c, "viewer@a.test")
    today = auth.utcnow().date()
    url = "/institutions/1/promises"
    assert c.post(url, headers=viewer, json={"loan_no": "L2", "amount_kes": 3000, "due_on": str(today)}).status_code == 403
    assert c.post(url, headers=officer, json={"loan_no": "L2", "amount_kes": 3000,
                                              "due_on": str(today + timedelta(days=90))}).status_code == 422
    p = c.post(url, headers=officer, json={"loan_no": "L2", "amount_kes": 3000, "due_on": str(today + timedelta(days=7))}).json()
    assert p["state"] == "open"
    assert c.post(url, headers=officer, json={"loan_no": "L2", "amount_kes": 1, "due_on": str(today)}).status_code == 409

    q = c.post("/institutions/1/collections/queue", headers=officer).json()
    assert q["waiting_on_promises"] == 1
    with Session() as s:
        assert s.scalar(select(Reminder).where(Reminder.loan_id == 2)) is None  # left alone while promised

    _pay(Session, 2, 3_000_00, datetime.combine(today, datetime.min.time()) + timedelta(hours=9))
    assert c.get(url, headers=viewer).json()[0]["state"] == "kept"

    with Session() as s:  # an old promise nobody paid against
        from sawazi.models import PromiseToPay
        s.add(PromiseToPay(institution_id=1, loan_id=1, amount_cents=1_000_00, made_on=today - timedelta(days=20),
                           due_on=today - timedelta(days=10), created_at=datetime.now()))
        s.commit()
    states = {x["loan_no"]: x["state"] for x in c.get(url, headers=viewer).json()}
    assert states == {"L1": "broken", "L2": "kept"}

    books = {o["officer"]: o for o in c.get("/institutions/1/officers", headers=viewer).json()}
    assert books["Kamau"]["par30_pct"] == 100.0 and books["Kamau"]["promises_broken"] == 1
    assert books[None]["collected_this_month_kes"] == 3000 and books[None]["promises_kept"] == 1


def test_insiders_are_listed_flagged_in_appraisal_and_in_the_board_pack(env):
    c, Session = env
    _book(Session)
    r = c.get("/institutions/1/insiders", headers=login(c, "viewer@a.test")).json()
    assert r["insiders"] == 1 and r["loans"][0]["insider"] == "Board member"
    with Session() as s:
        ins = boardpack.snapshot_figures(s, 1)["insiders"]
    assert (ins["loans"], ins["balance_cents"], ins["behind"]) == (1, 50_000_00, 1)

    a = appraisal.appraise(100_00, 6, appraisal.Product(1, 10**9, 12, 1200, guarantor_cover="none", one_third_rule=False,
                                                        deposits_multiplier_pct=0, min_membership_months=0),
                           appraisal.MemberFacts(None, None, None, None, insider="staff"), [], [], date(2026, 10, 13))
    assert a.outcome == "passes" and a.checks[-1].code == "insider" and a.checks[-1].status == "warn"  # never blocks

    up = c.post("/institutions/1/import/member_balances", headers=login(c, "admin@a.test"),
                files={"file": ("b.csv", b"Member No,Insider\nM2,Supervisory Committee\nM1,none\n")})
    assert up.status_code == 200
    with Session() as s:
        assert (s.get(Member, 2).insider, s.get(Member, 1).insider) == ("supervisory", None)
