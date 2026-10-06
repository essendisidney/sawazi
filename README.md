# Sawazi by Pesara

Repayment matching, check-off reconciliation and collections for SACCOs and microfinance institutions.
It sits beside any core banking system and works from the CSV/Excel exports every system can produce.

## What Phase 1 does

| Module | What it solves |
|---|---|
| **Importers** | Members and loans from the core system; M-Pesa paybill statements; bank statements; employer check-off schedules and remittances. Re-importing a file never double-counts. |
| **Matching engine** | Finds the member behind every payment using member number, loan number, ID number, bank narrative, registered phone and (as a suggestion only) payer name. Catches typos that land on *another* valid member number. Anything uncertain goes to suspense with a suggested member and the reason. |
| **Allocation** | Splits each payment: arrears first (oldest loan first), then the current instalment, then deposits. Exports a postings file for the core system. |
| **Check-off reconciliation** | Schedule vs remittance per employer and period: short, over, missing, unscheduled and unidentifiable payroll lines. Matches name-only payroll lines. |
| **Anomalies** | Possible double payments, third-party payments, unusually large payments (AML). |
| **Collections** | Ranks every loan in arrears by how much acting now recovers; queues SMS, call, guarantor notice, field visit or recovery, with the message drafted. Portfolio-at-risk (PAR 1/30/90). |

## Results on the sample SACCO (fictional data, `sample_data/`)

320 members, 259 loans, a month of M-Pesa, bank and check-off from two employers.

- 93% of payments matched and allocated automatically
- **100% of auto-allocated M-Pesa payments went to the right member** (checked against an answer key); 3 typos that pointed at another real member were caught, not misposted
- Check-off: 94.5% and 98.3% collection rates, with every short, missing and unidentified line listed
- PAR 1 fell from 35% to 17% once September's payments were applied

## Run it

```bash
pip install -r requirements.txt
python scripts/make_sample_data.py      # fictional test data
python scripts/run_demo.py              # full pipeline -> demo_output.json
python -m pytest -q                     # 15 tests
uvicorn sawazi.api:app --reload          # API at http://localhost:8000/docs
```

Uses SQLite locally. For production set `SAWAZI_DB_URL=postgresql+psycopg://...` and `SAWAZI_API_KEY`.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/institutions` | Create a SACCO/MFI tenant |
| POST | `/institutions/{id}/import/{kind}` | `members`, `loans`, `mpesa`, `bank`, `checkoff_schedule`, `checkoff_remittance` (check-off needs `employer` and `period=YYYY-MM`) |
| POST | `/institutions/{id}/checkoff/reconcile` | Reconcile one employer and period |
| POST | `/institutions/{id}/match` | Match and allocate everything pending |
| GET | `/institutions/{id}/exceptions` | Suspense items and flags, worst first |
| POST | `/exceptions/{id}/resolve` | Clear suspense to a member, or close a flag |
| POST | `/institutions/{id}/collections/queue` | Build the collections queue |
| GET | `/institutions/{id}/reminders` | Ranked actions with drafted messages |
| GET | `/institutions/{id}/dashboard` | Match rate, open exceptions, PAR |
| GET | `/institutions/{id}/exports/postings.csv` | Allocations for upload to the core system |

## Layout

```
sawazi/
  models.py            multi-tenant data model (money in integer cents)
  importers/           CSV parsing for every source
  engine/matching.py   member matching, allocation, anomaly flags
  engine/checkoff.py   check-off reconciliation
  engine/collections.py arrears ranking, messages, PAR
  api.py               FastAPI app
scripts/               sample data generator and demo run
tests/                 pytest suite
```

## Next (Phase 1 remaining)

- Send SMS through Africa's Talking, with delivery status
- Daraja C2B callbacks for real-time matching (statements stay as the fallback)
- Staff login, roles and an audit log of every manual action
- Staff web console for suspense clearing and the collections queue
- Allocation rules configurable per institution (penalties, interest, principal order)

Before any real member data: ODPC registration and a data processing agreement with each pilot institution.
