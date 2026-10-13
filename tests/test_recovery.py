from sawazi import boardpack
from sawazi.models import AuditEvent, Loan
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)
from tests.test_member_app import bearer, members, sign_up  # noqa: F401  (members is a fixture)


def test_recovery_tracks_agent_recoveries_and_guarantor_refunds(members):
    c, Session, phone = members
    officer, viewer = login(c, "credit_officer@a.test"), login(c, "viewer@a.test")
    with Session() as s:
        s.add(Loan(id=60, institution_id=1, member_id=12, loan_no="L60", principal_cents=10_000_000,
                   balance_cents=6_000_000, installment_cents=500_000, arrears_cents=3_000_000, days_in_arrears=120))
        s.commit()
    url = "/institutions/1/recoveries"
    body = {"loan_no": "L60", "stage": "debt_collector", "agent": "Haki Collectors", "handed_over_on": "2026-09-01"}
    assert c.post(url, headers=viewer, json=body).status_code == 403
    r = c.post(url, headers=officer, json=body).json()
    assert r["stage"] == "debt_collector" and r["days_with_agent"] > 30
    assert c.post(url, headers=officer, json=body).status_code == 409  # one recovery per loan

    one = f"{url}/{r['id']}"
    assert c.post(f"{one}/guarantors", headers=officer,
                  json={"member_no": "M3", "used_kes": 100, "used_on": "2026-09-10"}).status_code == 422  # the borrower
    out = c.post(f"{one}/guarantors", headers=officer,
                 json={"member_no": "M1", "used_kes": 20_000, "used_on": "2026-09-10"}).json()
    gid = out["guarantors"][0]["id"]
    assert out["refund_due_kes"] == 0  # nothing recovered yet

    c.put(one, headers=officer, json={**body, "recovered_kes": 15_000})
    rows = c.get(url, headers=viewer).json()
    assert rows[0]["refund_due_kes"] == 15_000  # recovered money goes back to the guarantor first

    bad = c.put(f"{one}/guarantors/{gid}", headers=officer,
                json={"used_kes": 20_000, "used_on": "2026-09-10", "refunded_kes": 25_000, "refunded_on": "2026-10-01"})
    assert bad.status_code == 422
    c.put(f"{one}/guarantors/{gid}", headers=officer,
          json={"used_kes": 20_000, "used_on": "2026-09-10", "refunded_kes": 15_000, "refunded_on": "2026-10-01"})
    assert c.get(one, headers=viewer).json()["refund_due_kes"] == 0

    with Session() as s:
        assert s.query(AuditEvent).filter(AuditEvent.action.like("recovery.%")).count() == 4
        rec = boardpack.snapshot_figures(s, 1)["recovery"]
    assert (rec["open"], rec["recovered_cents"], rec["refund_due_cents"]) == (1, 1_500_000, 0)

    tok = bearer(sign_up(c, phone)["session_token"])  # M1, the guarantor, sees their own deposits used
    used = c.get("/m/guarantees", headers=tok).json()["deposits_used"]
    assert used == [{"for": "Someone E.", "used_kes": 20_000, "used_on": "2026-09-10", "refunded_kes": 15_000,
                     "refunded_on": "2026-10-01", "being_recovered": True}]
