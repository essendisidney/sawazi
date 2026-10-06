"""Run the full Sawazi pipeline on the sample data and write a JSON summary.

python scripts/run_demo.py  ->  demo_output.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from sawazi.db import Base  # noqa: E402
from sawazi.engine.checkoff import reconcile_checkoff  # noqa: E402
from sawazi.engine.collections import build_queue, portfolio_at_risk  # noqa: E402
from sawazi.engine.matching import run_matching  # noqa: E402
from sawazi.importers import sources  # noqa: E402
from sawazi.models import ExceptionItem, Institution, Loan, Member, Reminder, Transaction  # noqa: E402

DATA = ROOT / "sample_data"


def main():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()

    inst = Institution(name="Ufanisi Teachers SACCO", kind="sacco", paybill="522900")
    s.add(inst)
    s.commit()
    iid = inst.id
    rd = lambda n: (DATA / n).read_bytes()  # noqa: E731

    par_before = portfolio_at_risk(s, iid)
    out = {"imports": {
        "members": sources.import_members(s, iid, rd("members.csv")).as_dict(),
        "loans": sources.import_loans(s, iid, rd("loans.csv")).as_dict(),
        "mpesa": sources.import_mpesa_statement(s, iid, rd("mpesa_statement.csv")).as_dict(),
        "mpesa_reimport": sources.import_mpesa_statement(s, iid, rd("mpesa_statement.csv")).as_dict(),
        "bank": sources.import_bank_statement(s, iid, rd("bank_statement.csv")).as_dict(),
    }}
    par_before = portfolio_at_risk(s, iid)

    out["checkoff"] = []
    for emp, tag in [("Mwangaza County Payroll", "county"), ("Tumaini Schools Ltd", "tumaini")]:
        sources.import_checkoff_schedule(s, iid, emp, "2026-09", rd(f"checkoff_schedule_{tag}.csv"))
        sources.import_checkoff_remittance(s, iid, emp, "2026-09", rd(f"checkoff_remittance_{tag}.csv"))
        out["checkoff"].append(reconcile_checkoff(s, iid, emp, "2026-09"))

    out["matching"] = run_matching(s, iid)
    out["collections"] = build_queue(s, iid)
    out["portfolio_before"] = par_before
    out["portfolio_after"] = portfolio_at_risk(s, iid)

    # Detail for the dashboard
    sev = {"high": 0, "medium": 1, "low": 2}
    exc = sorted(s.scalars(select(ExceptionItem).where(ExceptionItem.status == "open")),
                 key=lambda e: (sev[e.severity], -e.amount_cents))
    out["exceptions"] = [
        {"kind": e.kind, "severity": e.severity, "amount_kes": e.amount_cents / 100, "detail": e.detail}
        for e in exc
    ]
    rows = s.execute(
        select(Reminder, Loan, Member).join(Loan, Reminder.loan_id == Loan.id).join(Member, Loan.member_id == Member.id)
        .order_by(Reminder.priority_score.desc())
    ).all()
    out["reminders"] = [
        {"priority": r.priority_score, "channel": r.channel, "member": m.name, "member_no": m.member_no,
         "loan_no": ln.loan_no, "dpd": ln.days_in_arrears, "arrears_kes": ln.arrears_cents / 100,
         "via": ln.repays_via, "message": r.message}
        for r, ln, m in rows
    ]
    methods = {}
    for t in s.scalars(select(Transaction)):
        k = t.match_method or "none"
        methods.setdefault(k, {"count": 0, "status": {}})
        methods[k]["count"] += 1
        methods[k]["status"][t.status] = methods[k]["status"].get(t.status, 0) + 1
    out["match_methods"] = methods

    # Accuracy against the answer key: of what was auto-allocated, how much went to the right member?
    import csv as _csv
    truth = {r["receipt"]: r["true_member_no"] for r in _csv.DictReader(open(DATA / "mpesa_truth.csv"))}
    members = {mm.id: mm.member_no for mm in s.scalars(select(Member))}
    right = wrong = susp = 0
    wrong_list = []
    for t in s.scalars(select(Transaction).where(Transaction.source == "mpesa")):
        if t.reference not in truth:
            continue
        if t.status == "allocated":
            if members[t.member_id] == truth[t.reference]:
                right += 1
            else:
                wrong += 1
                wrong_list.append((t.reference, t.account_ref, t.match_method, members[t.member_id], truth[t.reference]))
        else:
            susp += 1
    out["accuracy"] = {"auto_allocated_correct": right, "auto_allocated_wrong": wrong, "sent_to_suspense": susp,
                       "precision_pct": round(100 * right / max(right + wrong, 1), 2), "wrong": wrong_list}
    print("ACCURACY", out["accuracy"])

    (ROOT / "demo_output.json").write_text(json.dumps(out, indent=2, default=str))
    m = out["matching"]
    print(f"processed={m['processed']} allocated={m['allocated']} suspense={m['suspense']} "
          f"auto_match={m['auto_match_rate']}% anomalies={m['anomalies']}")
    for c in out["checkoff"]:
        print({k: c[k] for k in ("employer", "expected_kes", "remitted_kes", "collection_rate_pct", "exact", "short", "missing", "over", "unidentified")})
    print("collections", out["collections"])
    print("PAR before", par_before)
    print("PAR after ", out["portfolio_after"])
    print("methods", json.dumps(methods))


if __name__ == "__main__":
    main()
