"""Register Sawazi's C2B callback URLs for one paybill, and (sandbox only) simulate a payment.

Credentials come from the environment, never the command line or the database:
  SAWAZI_DARAJA_ENV              sandbox (default) | production
  SAWAZI_DARAJA_CONSUMER_KEY     from the institution's Daraja app
  SAWAZI_DARAJA_CONSUMER_SECRET
  SAWAZI_DARAJA_CALLBACK_TOKEN   the same secret the API checks

Usage:
  python scripts/daraja_register.py register --shortcode 600000 --host https://sawazi.example.co.ke
  python scripts/daraja_register.py simulate --shortcode 600000 --amount 100 --account UT00104

Make sure the institution's paybill in Sawazi equals the shortcode, or callbacks are ignored.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sawazi.daraja import DarajaClient  # noqa: E402

SANDBOX_TEST_MSISDN = "254708374149"  # Safaricom's published sandbox test number


def env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"set {name} first")
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    reg = sub.add_parser("register", help="register confirmation and validation URLs")
    reg.add_argument("--shortcode", required=True)
    reg.add_argument("--host", required=True, help="public https base URL of the Sawazi API")
    sim = sub.add_parser("simulate", help="sandbox only: make a test payment")
    sim.add_argument("--shortcode", required=True)
    sim.add_argument("--amount", type=int, required=True, help="whole shillings")
    sim.add_argument("--account", required=True, help="BillRefNumber, e.g. a member number")
    sim.add_argument("--msisdn", default=SANDBOX_TEST_MSISDN)
    args = ap.parse_args(argv)

    client = DarajaClient(env("SAWAZI_DARAJA_CONSUMER_KEY"), env("SAWAZI_DARAJA_CONSUMER_SECRET"),
                          os.getenv("SAWAZI_DARAJA_ENV", "sandbox"))
    if args.cmd == "register":
        base = f"{args.host.rstrip('/')}/callbacks/c2b/{env('SAWAZI_DARAJA_CALLBACK_TOKEN')}"
        out = client.register_urls(args.shortcode, f"{base}/confirmation", f"{base}/validation")
    else:
        out = client.simulate(args.shortcode, args.amount, args.msisdn, args.account)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
