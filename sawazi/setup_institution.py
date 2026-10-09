"""Set up a SACCO on a running Sawazi server: the institution and its first admin.

On the server:   docker compose exec app python -m sawazi.setup_institution
It asks for the details, and for the admin's password without showing it. It uses the platform key from the
server's settings (SAWAZI_API_KEY), so the key is never typed or put on a command line. After this, the admin
adds everyone else in the console (Admin > Staff), and the platform key can be removed from deploy/.env.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.error
import urllib.request

API = os.getenv("SAWAZI_SETUP_URL", "http://127.0.0.1:8000")


def call(path: str, body: dict) -> dict:
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": os.environ["SAWAZI_API_KEY"]})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Refused ({e.code}): {json.load(e).get('detail', e.reason)}") from None


def ask(label: str, required: bool = True) -> str:
    while True:
        v = input(f"{label}: ").strip()
        if v or not required:
            return v
        print("  This is needed.")


def main() -> None:
    if not os.getenv("SAWAZI_API_KEY"):
        sys.exit("SAWAZI_API_KEY is not set on this server. Add it to deploy/.env, run `docker compose up -d`, try again.")
    print("Set up a SACCO on Sawazi. Press Ctrl+C to stop; nothing is saved until all the questions are answered.\n")
    name = ask("SACCO name (as members know it)")
    paybill = ask("M-Pesa paybill number (leave empty if none)", required=False) or None
    demo = ask("Is this a demo with fictional data only? (y/N)", required=False).lower().startswith("y")
    admin_name = ask("First admin's full name")
    email = ask("First admin's work email (their login)")
    while True:
        pw = getpass.getpass("First admin's password (at least 10 characters, not shown): ")
        if len(pw) < 10:
            print("  At least 10 characters.")
        elif pw != getpass.getpass("Type it again: "):
            print("  The two passwords are different.")
        else:
            break
    inst = call("/institutions", {"name": name, "paybill": paybill, "is_demo": demo})
    try:
        call(f"/institutions/{inst['id']}/admin", {"email": email, "name": admin_name, "role": "admin", "password": pw})
    except SystemExit as e:
        sys.exit(f"{e}\n{name} was created as institution {inst['id']}, but without an admin. Do not run this again "
                 f"(it would create a second {name}); ask Pesara to add the admin to institution {inst['id']}.")
    print(f"\nDone. {name} is institution {inst['id']}. {admin_name} can log in at /console/ as {email.lower()}.")
    print("At their first login they choose their own password. Then they add staff under Admin > Staff.")
    print("Finally remove SAWAZI_API_KEY from the server settings and restart.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nStopped. Nothing was saved.")
