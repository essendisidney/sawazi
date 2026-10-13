from datetime import date, timedelta

from sqlalchemy import select

from sawazi import auth, boardpack
from sawazi.models import (AuditEvent, CheckoffRemittance, CheckoffSchedule, ExceptionItem, LoanApplication,
                           LoanDecision, LoanProduct, Member, StaffUser, Transaction)
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


def test_employer_scorecard_shows_gaps_people_left_off_and_lateness(env):
    c, Session = env
    with Session() as s:
        s.add_all([Member(id=i, institution_id=1, member_no=f"M{i}", name=f"Member {i}") for i in (1, 2, 3)])
        s.flush()
        for per, received in (("2026-08", date(2026, 9, 3)), ("2026-09", date(2026, 10, 20))):
            for mid in (1, 2, 3):
                s.add(CheckoffSchedule(institution_id=1, employer="Mwangaza County", period=per, member_id=mid,
                                       expected_cents=5_000_00))
            for mid in ((1, 2, 3) if per == "2026-08" else (1, 2)):
                s.add(CheckoffRemittance(institution_id=1, employer="Mwangaza County", period=per, member_id=mid,
                                         remitted_cents=5_000_00, received_on=received))
        s.commit()
    e = c.get("/institutions/1/employers", headers=login(c, "viewer@a.test")).json()[0]
    assert e["employer"] == "Mwangaza County" and (e["months_short"], e["months_late"]) == (1, 1)
    sep, aug = e["months"]
    assert (sep["period"], sep["gap_kes"], sep["members_left_off"], sep["days_late"]) == ("2026-09", 5000, 1, 20)
    assert (aug["gap_kes"], aug["days_late"]) == (0, 3)


def test_suspense_ageing_and_integrity_checks(env):
    c, Session = env
    now = auth.utcnow()
    with Session() as s:
        s.add_all([Member(id=1, institution_id=1, member_no="M1", name="Achieng Owino", id_number="22334455",
                          phone="254711000001", insider="board"),
                   Member(id=2, institution_id=1, member_no="M2", name="A Owino", id_number="022334455",
                          phone="254711000001"),
                   Member(id=3, institution_id=1, member_no="M3", name="Kamau", phone="254711000001")])
        s.flush()
        for i, age in enumerate((2, 45, 120)):
            t = Transaction(institution_id=1, source="mpesa", reference=f"S{i}", amount_cents=1_000_00,
                            txn_time=now - timedelta(days=age), status="suspense")
            s.add(t)
            s.flush()
            s.add(ExceptionItem(institution_id=1, kind="suspense", transaction_id=t.id, amount_cents=1_000_00,
                                detail="no match", status="open"))
        p = LoanProduct(institution_id=1, code="DEV", name="Dev", min_amount_cents=1, max_amount_cents=10**9,
                        max_term_months=12, interest_rate_bps=1200, interest_method="reducing",
                        deposits_multiplier_pct=0, min_membership_months=0, max_arrears_days=30, one_third_rule=False,
                        guarantor_cover="none", min_guarantors=0, created_at=now, updated_at=now)
        s.add(p)
        s.flush()
        approver = s.scalar(select(StaffUser).where(StaffUser.email == "approver@a.test"))
        for i in range(3):
            a = LoanApplication(institution_id=1, member_id=3, product_id=p.id, amount_cents=1_000_00, term_months=6,
                                status="approved", override_reason="committee knows the member well",
                                created_at=now, submitted_at=now - timedelta(minutes=10))
            s.add(a)
            s.flush()
            s.add(LoanDecision(institution_id=1, application_id=a.id, user_id=approver.id, decision="approve",
                               appraisal_outcome="fails", at=now))
        sunday = (now - timedelta(days=(now.weekday() - 6) % 7 or 7)).replace(hour=20, minute=30)
        s.add(AuditEvent(institution_id=1, at=sunday, actor_kind="user", actor_id=1,
                         actor_name="Night Clerk", action="suspense.clear", entity_type="exception", entity_id=1,
                         after={"member_no": "M1"}))  # Sunday 23:30 in Nairobi, to an insider
        s.commit()
    viewer = login(c, "viewer@a.test")
    age = c.get("/institutions/1/suspense/ageing", headers=viewer).json()
    assert [b["count"] for b in age["buckets"]] == [1, 0, 1, 1] and age["over_30"] == {"count": 2, "kes": 2000}
    assert age["oldest"][0]["age_days"] == 120

    r = c.get("/institutions/1/integrity", headers=viewer).json()
    kinds = {f["kind"] for f in r["findings"]}
    assert {"duplicate_id", "shared_phone", "override_habit", "quick_override", "after_hours"} <= kinds
    assert "insider_clearance" in kinds
    assert r["findings"][0]["severity"] == "high"
    assert r["approvers"][0]["override_pct"] == 100.0

    with Session() as s:
        w = boardpack.snapshot_figures(s, 1)["watch"]
    assert w["suspense_over_30"] == {"count": 2, "cents": 2_000_00} and w["integrity_high"] >= 1
