import re
from datetime import datetime
from pathlib import Path

from sawazi.models import ExceptionItem, Loan, Member, Reminder, Transaction
from tests.test_auth import env, login  # noqa: F401  (env is a fixture)

CONSOLE = Path(__file__).resolve().parent.parent / "sawazi" / "console"


# ---------------------------------------------------------------- serving

def test_console_served_with_strict_headers(env):
    c, _ = env
    r = c.get("/console/")
    assert r.status_code == 200 and "console.js" in r.text
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    assert c.get("/console/console.js").status_code == 200
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 200 and 'href="/console/"' in r.text  # the landing page links to the console
    assert "content-security-policy" not in c.get("/docs").headers  # API docs unaffected


def test_console_never_renders_data_as_html():
    """Member names and narratives come from uploaded files: they must only ever become text."""
    js = (CONSOLE / "console.js").read_text(encoding="utf-8")
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert risky not in js, risky
    html = (CONSOLE / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html  # scripts only from files, as the CSP requires
    assert not re.search(r"<[^>]+\son[a-z]+\s*=", html)  # no inline event handlers


# ---------------------------------------------------------------- what the console reads

def test_me_lists_permissions_and_institution(env):
    c, _ = env
    viewer = c.get("/auth/me", headers=login(c, "viewer@a.test")).json()
    assert viewer["institution_name"] == "SACCO A" and viewer["permissions"] == ["read"]
    officer = c.get("/auth/me", headers=login(c, "credit_officer@a.test")).json()
    assert set(officer["permissions"]) == {"read", "collections", "send_sms", "loan_apply"}
    admin = c.get("/auth/me", headers=login(c, "admin@a.test")).json()
    assert {"manage_users", "resolve", "audit", "sms_settings"} <= set(admin["permissions"])


def seed_people(Session):
    with Session() as s:
        s.add_all([
            Member(id=10, institution_id=1, member_no="UT00104", name="Achieng Owino", phone="254711000001",
                   id_number="23456789"),
            Member(id=11, institution_id=1, member_no="UT00200", name="Grace Wafula", phone="254711000003"),
            Member(id=20, institution_id=2, member_no="UT00104", name="Achieng Owino (B)", phone="254711000001"),
            Loan(id=10, institution_id=1, member_id=10, loan_no="LN1", principal_cents=10_000_000,
                 balance_cents=5_000_000, installment_cents=500_000, arrears_cents=300_000, days_in_arrears=20),
            Transaction(id=10, institution_id=1, source="mpesa", reference="QX1", txn_time=datetime(2026, 9, 5, 10),
                        amount_cents=300_000, status="suspense", account_ref="UT0O104", payer_name="ACHIENG O",
                        payer_phone="254711000001"),
        ])
        s.flush()  # parents before children: PostgreSQL checks foreign keys
        s.add_all([
            ExceptionItem(id=10, institution_id=1, kind="suspense", transaction_id=10, member_id=10,
                          amount_cents=300_000, detail="typo in account; suggested UT00104"),
            Reminder(id=10, institution_id=1, loan_id=10, channel="sms", priority_score=60, message="Dear Achieng"),
            Reminder(id=11, institution_id=1, loan_id=10, channel="recovery", priority_score=30, message="internal"),
        ])
        s.commit()


def test_exceptions_carry_payment_and_suggested_member(env):
    c, Session = env
    seed_people(Session)
    [e] = c.get("/institutions/1/exceptions", headers=login(c, "viewer@a.test"), params={"kind": "suspense"}).json()
    assert e["transaction"]["reference"] == "QX1" and e["transaction"]["account_ref"] == "UT0O104"
    assert e["transaction"]["payer_phone"] == "254711000001" and e["transaction"]["amount_kes"] == 3000
    assert e["suggested_member"] == {"member_no": "UT00104", "name": "Achieng Owino", "phone": "254711000001"}
    flag = [x for x in c.get("/institutions/1/exceptions", headers=login(c, "viewer@a.test")).json() if x["id"] == 1][0]
    assert flag["transaction"] is None and flag["suggested_member"] is None  # older fields unchanged


def test_member_search(env):
    c, Session = env
    seed_people(Session)
    h = login(c, "viewer@a.test")
    find = lambda q: c.get("/institutions/1/members", headers=h, params={"q": q}).json()  # noqa: E731
    [m] = find("UT00104")
    assert m["name"] == "Achieng Owino" and m["loans"][0]["loan_no"] == "LN1" and m["loans"][0]["arrears_kes"] == 3000
    assert [x["member_no"] for x in find("wafula")] == ["UT00200"]
    assert [x["member_no"] for x in find("0711 000 001")] == ["UT00104"]  # phone in any format
    assert [x["member_no"] for x in find("23456789")] == ["UT00104"]  # ID number
    assert all("(B)" not in x["name"] for x in find("Achieng"))  # never another institution's members
    assert c.get("/institutions/1/members", headers=h, params={"q": "a"}).status_code == 422
    assert c.get("/institutions/2/members", headers=h, params={"q": "UT00104"}).status_code == 404


def test_reminders_say_which_can_be_texted(env):
    c, Session = env
    seed_people(Session)
    h = login(c, "viewer@a.test")
    rows = {r["id"]: r for r in c.get("/institutions/1/reminders", headers=h).json()}
    assert rows[10]["sms_allowed"] is True and rows[11]["sms_allowed"] is False
    assert rows[10]["status"] == "queued"
    assert c.get("/institutions/1/reminders", headers=h, params={"status": "failed"}).json() == []
    assert c.get("/institutions/1/reminders", headers=h, params={"status": "sent"}).status_code == 422


def test_console_script_parses():
    """A syntax error blanks the whole console; catch it here (needs Node.js, skipped without it)."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("Node.js not installed")
    r = subprocess.run([node, "--check", str(CONSOLE / "console.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_landing_page_carries_the_company_line_and_the_apps_do_not():
    """'A product of Pesara Limited' belongs in the landing page footer only, never on an app screen."""
    from fastapi.testclient import TestClient

    from sawazi.api import app

    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    footer = r.text.split("<footer>", 1)[1]
    assert "A product of Pesara Limited" in footer and r.text.count("Pesara") == 1
    assert 'href="/console/"' in r.text and 'href="/app/"' in r.text
    csp = r.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "script" not in csp and "<script" not in r.text
    assert c.get("/site/site.css").status_code == 200
    root = Path(__file__).resolve().parent.parent / "sawazi"
    for f in ["console/console.js", "console/index.html", "app/app.js", "app/index.html", "boardpack.py"]:
        assert "Pesara Limited" not in (root / f).read_text(encoding="utf-8").replace("PESARA LIMITED", "Pesara Limited"), f
