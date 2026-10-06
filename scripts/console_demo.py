"""Run the staff console locally on fictional sample data.

    python scripts/make_sample_data.py      # once, if sample_data/ is empty
    python scripts/console_demo.py          # then open http://localhost:8765/console/

Creates console_demo.db (gitignored) the first time, with one SACCO, a login per role and
September's sample payments loaded, matched and queued. Delete the file to start again.
SMS stays in simulate mode: nothing is sent. Never use these logins anywhere real.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "console_demo.db"
DATA = ROOT / "sample_data"
PORT = int(os.getenv("PORT", "8765"))

os.environ["SAWAZI_DB_URL"] = f"sqlite:///{DB.as_posix()}"
os.environ.setdefault("SAWAZI_API_KEY", "console-demo-platform-key")
os.environ["SAWAZI_SMS_PROVIDER"] = "simulate"
sys.path.insert(0, str(ROOT))

DEMO_PASSWORD = "demo-password-123"  # fictional demo data only
DEMO_USERS = [("admin", "Wanjiru Admin"), ("accountant", "Otieno Accountant"),
              ("credit_officer", "Chebet Credit Officer"), ("viewer", "Mutua Viewer")]


def seed():
    from fastapi.testclient import TestClient

    from sawazi.api import app

    with TestClient(app) as c:
        pk = {"X-API-Key": os.environ["SAWAZI_API_KEY"]}
        iid = c.post("/institutions", headers=pk, json={"name": "Ufanisi Teachers SACCO", "paybill": "522900"}).json()["id"]
        users = [{"email": f"{role.replace('_', '.')}@ufanisi.test", "name": name, "role": role, "password": DEMO_PASSWORD}
                 for role, name in DEMO_USERS]
        c.post(f"/institutions/{iid}/admin", headers=pk, json=users[0]).raise_for_status()
        tok = c.post("/auth/login", json={"email": users[0]["email"], "password": DEMO_PASSWORD}).json()["token"]
        c.headers["Authorization"] = f"Bearer {tok}"
        for u in users[1:]:
            c.post(f"/institutions/{iid}/users", json=u).raise_for_status()

        def upload(kind, name, **params):
            r = c.post(f"/institutions/{iid}/import/{kind}", params=params,
                       files={"file": (name, (DATA / name).read_bytes())})
            r.raise_for_status()

        for kind, name in [("members", "members.csv"), ("loans", "loans.csv"), ("mpesa", "mpesa_statement.csv"),
                           ("bank", "bank_statement.csv")]:
            upload(kind, name)
        for slug, employer in [("county", "Mwangaza County Payroll"), ("tumaini", "Tumaini Schools Ltd")]:
            upload("checkoff_schedule", f"checkoff_schedule_{slug}.csv", employer=employer, period="2026-09")
            upload("checkoff_remittance", f"checkoff_remittance_{slug}.csv", employer=employer, period="2026-09")
            c.post(f"/institutions/{iid}/checkoff/reconcile", params={"employer": employer, "period": "2026-09"})
        c.post(f"/institutions/{iid}/match").raise_for_status()
        c.post(f"/institutions/{iid}/collections/queue").raise_for_status()
        c.post("/auth/logout")


def main():
    if not (DATA / "members.csv").exists():
        sys.exit("No sample data yet: run python scripts/make_sample_data.py first")
    if not DB.exists():
        print("Creating console_demo.db with fictional sample data...")
        seed()
    print(f"\nStaff console: http://localhost:{PORT}/console/")
    print("Logins (fictional demo data; password is DEMO_PASSWORD in scripts/console_demo.py):")
    for role, _ in DEMO_USERS:
        print(f"  {role:15} {role.replace('_', '.')}@ufanisi.test")
    import uvicorn
    uvicorn.run("sawazi.api:app", host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
