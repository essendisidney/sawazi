import json
from datetime import datetime

import pytest
from sqlalchemy import select

from sawazi import audit
from sawazi.auth import Principal
from sawazi.models import AuditEvent, ExceptionItem, Loan, Member, Transaction
from tests.test_auth import PK, PW, env, login  # noqa: F401  (env is a fixture)


def events(Session, **where):
    with Session() as s:
        q = select(AuditEvent).order_by(AuditEvent.id)
        for k, v in where.items():
            q = q.where(getattr(AuditEvent, k) == v)
        return list(s.scalars(q))


@pytest.fixture()
def suspense(env):
    """A member with a loan in arrears and an M-Pesa payment sitting in suspense, in SACCO A."""
    _, Session = env
    with Session() as s:
        s.add(Member(id=10, institution_id=1, member_no="UT00104", name="Achieng Owino", phone="254711000001"))
        s.add(Loan(id=10, institution_id=1, member_id=10, loan_no="LN1", principal_cents=10_000_000,
                   balance_cents=5_000_000, installment_cents=500_000, arrears_cents=300_000, days_in_arrears=20))
        s.add(Transaction(id=10, institution_id=1, source="mpesa", reference="QX1", txn_time=datetime(2026, 9, 5),
                          amount_cents=300_000, status="suspense", account_ref="UT0O104"))
        s.flush()  # parents before children: PostgreSQL checks foreign keys
        s.add(ExceptionItem(id=10, institution_id=1, kind="suspense", transaction_id=10, amount_cents=300_000,
                            detail="account ref not found; suggested UT00104"))
        s.commit()
    return 10


# ---------------------------------------------------------------- manual money actions

def test_clearing_suspense_records_who_before_and_after(env, suspense):
    c, Session = env
    h = login(c, "accountant@a.test")
    me = c.get("/auth/me", headers=h).json()
    r = c.post(f"/exceptions/{suspense}/resolve", headers=h, json={"member_no": "UT00104", "note": "member called in"})
    assert r.status_code == 200

    [ev] = events(Session, action="suspense.clear")
    assert (ev.institution_id, ev.actor_kind, ev.actor_id, ev.actor_name) == (1, "user", me["id"], "accountant a")
    assert (ev.entity_type, ev.entity_id, ev.note) == ("exception", suspense, "member called in")
    assert ev.before["status"] == "open" and ev.before["transaction_status"] == "suspense"
    assert ev.before["transaction_member_id"] is None and ev.before["reference"] == "QX1"
    assert ev.after["status"] == "resolved" and ev.after["member_no"] == "UT00104"
    assert sum(a["amount_cents"] for a in ev.after["allocations"]) == 300_000
    assert ev.at is not None


def test_failed_suspense_clear_records_nothing(env, suspense):
    c, Session = env
    h = login(c, "accountant@a.test")
    assert c.post(f"/exceptions/{suspense}/resolve", headers=h, json={"member_no": "NOPE"}).status_code == 404
    assert events(Session, action="suspense.clear") == []


def test_resolving_a_flag_is_recorded(env):
    c, Session = env
    c.post("/exceptions/1/resolve", headers=login(c, "admin@a.test"), json={"note": "same payer, genuine top-up"})
    [ev] = events(Session, action="exception.resolve")
    assert ev.before["status"] == "open" and ev.after == {"status": "resolved"}
    assert ev.note == "same payer, genuine top-up"


# ---------------------------------------------------------------- access changes, never secrets

def test_user_changes_recorded_without_secrets(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    new = c.post("/institutions/1/users", headers=admin,
                 json={"email": "n@a.test", "name": "New", "role": "viewer", "password": "initial-pass-1"}).json()
    c.patch(f"/institutions/1/users/{new['id']}", headers=admin, json={"role": "accountant", "password": "reset-pass-22"})
    c.patch(f"/institutions/1/users/{new['id']}", headers=admin, json={"is_active": False})

    created = events(Session, action="user.create")[-1]
    assert created.entity_id == new["id"] and created.after["role"] == "viewer"
    upd = events(Session, action="user.update")
    assert upd[0].before == {"role": "viewer"} and upd[0].after == {"role": "accountant", "password_reset": True}
    assert upd[1].before == {"is_active": True} and upd[1].after == {"is_active": False}

    dump = json.dumps([[e.before, e.after, e.note] for e in events(Session)])
    for secret in ("initial-pass-1", "reset-pass-22", "scrypt$", "password_hash"):
        assert secret not in dump


def test_api_key_lifecycle_recorded_without_the_key(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    k = c.post("/institutions/1/api-keys", headers=admin, json={"name": "nightly sync", "role": "viewer"}).json()
    c.delete(f"/institutions/1/api-keys/{k['id']}", headers=admin)
    created, revoked = events(Session, action="api_key.create")[0], events(Session, action="api_key.revoke")[0]
    assert created.after == {"name": "nightly sync", "role": "viewer", "prefix": k["prefix"]}
    assert revoked.entity_id == k["id"] and revoked.after["revoked"] is True
    assert k["key"] not in json.dumps([[e.before, e.after] for e in events(Session)])


def test_secret_fields_are_stripped_whatever_the_caller_passes(env):
    _, Session = env
    with Session() as s:
        ev = audit.record(s, audit.platform(1), "test", before={"password": "x", "nested": [{"token": "t", "ok": 1}]},
                          after={"key": "swz_x", "when": datetime(2026, 1, 1)})
        assert ev.before == {"nested": [{"ok": 1}]} and ev.after == {"when": "2026-01-01 00:00:00"}


# ---------------------------------------------------------------- logins

def test_login_events(env):
    c, Session = env
    h = login(c, "viewer@a.test")
    c.post("/auth/login", json={"email": "viewer@a.test", "password": "wrong-password"})
    c.post("/auth/login", json={"email": "ghost@a.test", "password": "wrong-password"})
    c.post("/auth/password", headers=h, json={"current_password": PW, "new_password": "another-pass-1"})
    c.post("/auth/logout", headers=h)

    ok = events(Session, action="auth.login")[0]
    assert ok.actor_kind == "user" and ok.ip
    [failed] = events(Session, action="auth.login_failed")  # the unknown email is not logged anywhere
    assert failed.actor_kind == "anonymous" and failed.actor_id is None
    assert failed.entity_id == ok.actor_id and failed.note == "wrong password" and failed.ip
    assert len(events(Session, action="auth.password_change")) == 1
    assert len(events(Session, action="auth.logout")) == 1


# ---------------------------------------------------------------- runs, exports, platform

def test_runs_and_exports_record_the_api_key(env):
    c, Session = env
    admin = login(c, "admin@a.test")
    k = c.post("/institutions/1/api-keys", headers=admin, json={"name": "sync", "role": "accountant"}).json()
    key = {"Authorization": f"Bearer {k['key']}"}
    c.post("/institutions/1/import/members", headers=key,
           files={"file": ("members.csv", b"member_no,name,phone\nUT1,Jane Doe,0712000001\n")})
    c.post("/institutions/1/match", headers=key)
    c.post("/institutions/1/collections/queue", headers=key)
    c.get("/institutions/1/exports/postings.csv", headers=key)
    actions = [(e.action, e.actor_kind, e.actor_id, e.actor_name) for e in events(Session, actor_kind="api_key")]
    assert [a[0] for a in actions] == ["import.members", "match.run", "collections.queue", "export.postings"]
    assert {a[1:] for a in actions} == {("api_key", k["id"], "sync")}
    imp = events(Session, action="import.members")[0]
    assert imp.note == "file members.csv" and imp.after["created"] == 1


def test_platform_actions_recorded(env):
    c, Session = env
    iid = c.post("/institutions", headers=PK, json={"name": "New SACCO"}).json()["id"]
    c.post(f"/institutions/{iid}/admin", headers=PK,
           json={"email": "boss@new.test", "name": "Boss", "role": "admin", "password": "long-enough-pw"})
    evs = events(Session, institution_id=iid)
    assert [(e.action, e.actor_kind) for e in evs] == [("institution.create", "platform"), ("user.create", "platform")]


# ---------------------------------------------------------------- reading the log

def test_only_admin_reads_and_only_own_institution(env):
    c, _ = env
    c.post("/exceptions/2/resolve", headers=login(c, "admin@b.test"), json={"note": "B only"})
    admin_a = login(c, "admin@a.test")
    log = c.get("/institutions/1/audit", headers=admin_a).json()
    assert log and all(e["action"] != "exception.resolve" for e in log)
    assert c.get("/institutions/2/audit", headers=admin_a).status_code == 404
    assert c.get("/institutions/1/audit", headers=login(c, "accountant@a.test")).status_code == 403
    k = c.post("/institutions/1/api-keys", headers=admin_a, json={"name": "s", "role": "accountant"}).json()["key"]
    assert c.get("/institutions/1/audit", headers={"Authorization": f"Bearer {k}"}).status_code == 403


def test_audit_filters_and_paging(env, suspense):
    c, _ = env
    admin = login(c, "admin@a.test")
    for i in range(3):
        c.post("/institutions/1/users", headers=admin,
               json={"email": f"u{i}@a.test", "name": f"U{i}", "role": "viewer", "password": "long-enough-pw"})
    c.post(f"/exceptions/{suspense}/resolve", headers=admin, json={"member_no": "UT00104"})

    get = lambda **p: c.get("/institutions/1/audit", headers=admin, params=p).json()  # noqa: E731
    users = get(action="user.")
    assert [e["action"] for e in users] == ["user.create"] * 3
    assert users[0]["id"] > users[-1]["id"]  # newest first
    assert len(get(action="suspense.clear", entity_type="exception", entity_id=suspense)) == 1
    page1 = get(action="user.", limit=2)
    page2 = get(action="user.", limit=2, before_id=page1[-1]["id"])
    assert len(page1) == 2 and len(page2) == 1 and page2[0]["id"] < page1[-1]["id"]
    assert get(since="2100-01-01T00:00:00") == []


# ---------------------------------------------------------------- integrity

def test_audit_events_are_append_only(env):
    _, Session = env
    with Session() as s:
        audit.record(s, audit.platform(1), "test.event")
        s.commit()
        ev = s.scalar(select(AuditEvent).where(AuditEvent.action == "test.event"))
        ev.note = "tampered"
        with pytest.raises(RuntimeError, match="append-only"):
            s.commit()
        s.rollback()
        s.delete(s.get(AuditEvent, ev.id))
        with pytest.raises(RuntimeError, match="append-only"):
            s.commit()


def test_record_shares_the_callers_transaction(env):
    _, Session = env
    with Session() as s:
        audit.record(s, Principal("user", 1, 1, "admin", "x"), "test.rolled_back")
        s.rollback()
    assert events(Session, action="test.rolled_back") == []
