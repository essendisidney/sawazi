from datetime import datetime

from sawazi.models import AuditEvent, CoreGuarantee, Institution, Loan, Member
from sawazi.network import id_key
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)


def test_id_key():
    assert id_key(" 0012 345 678 ") == id_key("12345678") == "12345678"
    assert id_key(None) is None and id_key("123") is None  # too short to link safely


def _pledge(s, iid, loan_id, borrower, guarantor, cents):
    s.add(Loan(id=loan_id, institution_id=iid, member_id=borrower, loan_no=f"L{loan_id}", principal_cents=1,
               balance_cents=1, installment_cents=1))
    s.flush()
    s.add(CoreGuarantee(institution_id=iid, loan_id=loan_id, guarantor_member_id=guarantor, amount_cents=cents,
                        status="active", imported_at=datetime(2026, 10, 1)))


def test_network_shows_only_counts_and_totals_and_only_when_both_joined(env):
    c, Session = env
    with Session() as s:
        s.add_all([Member(id=1, institution_id=1, member_no="A1", name="Wanjiku Guarantor", id_number="22334455"),
                   Member(id=2, institution_id=1, member_no="A2", name="Borrower A"),
                   Member(id=3, institution_id=2, member_no="B1", name="Wanjiku G", id_number="022 334 455"),
                   Member(id=4, institution_id=2, member_no="B2", name="Borrower B")])
        s.flush()
        _pledge(s, 1, 10, 2, 1, 5_000_000)
        _pledge(s, 2, 20, 4, 3, 7_000_000)
        s.commit()
    admin, viewer = login(c, "admin@a.test"), login(c, "viewer@a.test")
    url = "/institutions/1/network"
    assert c.get(f"{url}/guarantees", headers=viewer).json() == {"opted_in": False, "saccos": 0, "members": []}
    assert c.put(url, headers=viewer, json={"opt_in": True, "confirm": "DPA"}).status_code == 403
    assert c.put(url, headers=admin, json={"opt_in": True}).status_code == 422  # must say what allows sharing
    assert c.put(url, headers=admin, json={"opt_in": True, "confirm": "Member consent clause 7"}).json()["opted_in"]
    assert c.get(f"{url}/guarantees", headers=viewer).json()["members"] == []  # the other SACCO has not joined

    with Session() as s:
        s.get(Institution, 2).network_opt_in = True
        s.commit()
    net = c.get(f"{url}/guarantees", headers=viewer).json()
    assert net["saccos"] == 2
    assert net["members"] == [{"member_no": "A1", "name": "Wanjiku Guarantor", "pledged_here_kes": 50_000,
                               "deposits_here_kes": None, "saccos_elsewhere": 1, "pledged_elsewhere_kes": 70_000,
                               "total_pledged_kes": 120_000}]  # no SACCO name, no member number from there

    with Session() as s:
        s.get(Institution, 2).is_demo = True  # demo data never mixes with real
        s.commit()
    assert c.get(f"{url}/guarantees", headers=viewer).json()["members"] == []
    with Session() as s:
        assert s.query(AuditEvent).filter(AuditEvent.action == "network.join").count() == 1
