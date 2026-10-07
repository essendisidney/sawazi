# Sawazi by Pesara

Sawazi is a product of Pesara (the house brand). It is an operations layer that sits beside any SACCO or
microfinance core banking system. It is NOT a core banking system and must never become one.
It never holds, moves or lends money. Keeping it read/compute-only keeps it out of CBK/SASRA licensing.

Owner: Sidney Essendi (product + domain lead, 15+ years SACCO/MFI core banking). Primary language: Python.

## The problem it solves (in priority order)
1. Arrears and collections: manual, late, unprioritised
2. Repayments that don't land cleanly: wrong paybill references, late/short employer check-off, suspense
3. Slow loans: paper applications, chasing guarantors
4. Hidden guarantor / group-liability exposure
5. Fraud and weak governance found late
6. Manual SASRA / CBK returns

## Roadmap
- Phase 1 DONE: repayment matching, check-off reconciliation, collections, console, SMS, C2B, PostgreSQL.
- Phase 2 DONE: loan factory (digital applications, appraisal), digital guarantor network (SMS link + USSD)
- Phase 3 (next): exposure/risk view, board pack, SASRA/CBK return generation, member app
- Phase 4: cross-institution network (guarantee exposure, sector benchmarks), MFI group lending, regional

## Phase 2 — done
1. Member balances and pay DONE (members export columns + `member_balances` import; unknown is never zero)
2. Loan products + appraisal engine DONE (`sawazi/engine/appraisal.py`, pure; never approves; unknown is never a pass)
3. Applications DONE: `approver` role, maker-checker, two approvers above a threshold, written override for
   failed/unknown checks, hand-over CSV to the core, disbursement linked only on an exact single match
4. Guarantors by SMS link + PIN DONE (`sawazi/guarantors.py`); capacity re-checked under a row lock on acceptance
5. Console: Loans, application page, Products DONE
6. Release on repayment DONE: a disbursed loan that closes (loans upload, matching, C2B, suspense clear) releases
   its guarantees (`guarantors.release_repaid`); written-off loans keep their guarantors liable
7. USSD consent DONE (`sawazi/ussd.py`): last 4 ID digits to accept; the shown list is saved per session so an
   answer always lands on the request that was read. Same `guarantors.record_answer` (and lock) as the web page
- Before go-live: Taifa USSD shortcode + confirm their callback format (built for the common sessionId/phoneNumber/
  text, CON/END format); native-speaker check of the Swahili on the guarantor page
- Moved to Phase 3: member self-service applications (member app)

## Phase 1 — done
1. Staff auth DONE: users, roles, per-institution scoping, per-institution API keys (`sawazi/auth.py`, role matrix in `PERMISSIONS`, human-only actions in `HUMAN_ONLY`)
2. Audit log DONE (`sawazi/audit.py`)
3. SMS DONE via Taifa Mobile (`sawazi/sms.py`; no Taifa sandbox, so `simulate` is the default provider). Delivery callbacks, opt-outs, staff-approval-only sending. Left: confirm with Taifa the number format (we send 2547XXXXXXXX) and API key length before the first live send
4. Daraja C2B DONE (`sawazi/daraja.py`, `scripts/daraja_register.py`). Validation always accepts; statement uploads confirm every callback. Left: test against the Daraja sandbox; if Safaricom sends hashed MSISDNs, consider matching on the hash of member phones
5. Staff web console DONE (`sawazi/console/`, run `scripts/console_demo.py`). Left: admin screens (staff users, API keys, SMS settings, opt-outs, audit log) still API-only
6. Allocation rules DONE (`sawazi/engine/allocation.py`). Penalty/interest arrears come from the core export, never computed by Sawazi. Defaults must keep reproducing the original split. Left: share-capital rules that need the member's share balance (e.g. 'until minimum shares reached') wait until the core export carries it
7. Alembic + PostgreSQL DONE (`migrations/`, `alembic upgrade head` on deploy; API also migrates on start). Suite passes on PostgreSQL 16 (`SAWAZI_TEST_DB_URL`)

## Stack and conventions
- FastAPI + SQLAlchemy 2.0 (typed `Mapped[]` models) + pydantic v2. SQLite locally, PostgreSQL in production via `SAWAZI_DB_URL`.
- Money is ALWAYS integer cents (`*_cents`, `BigInteger` columns). Never floats for stored money.
- Every table has `institution_id` (multi-tenant). Every query must filter by it. Never leak data across institutions.
- Imports must be idempotent: re-uploading a file never double-counts (unique on institution + source + reference).
- Matching never guesses: below confidence 85 a payment goes to suspense with a reason and a suggested member.
  Precision (money to the right member) matters more than match rate. A wrong posting is worse than suspense.
- Kenyan phone numbers normalised to 2547XXXXXXXX / 2541XXXXXXXX.
- Plain, specific messages to members and staff. No jargon in member SMS.
- Least-cost hosting: one small VPS or low-cost managed Postgres to start; scale only when pilots are live.

## Layout
- `sawazi/models.py` data model
- `sawazi/importers/` CSV parsing (members, loans, M-Pesa paybill statement, bank statement, check-off)
- `sawazi/engine/matching.py` member matching, anomaly flags
- `sawazi/engine/allocation.py` allocation rules: `plan()` is pure (used for preview), `apply()` writes
- `sawazi/engine/checkoff.py` check-off schedule vs remittance reconciliation
- `sawazi/engine/collections.py` arrears ranking, drafted messages, PAR
- `sawazi/api.py` FastAPI app
- `sawazi/console/` staff console, vanilla JS, no build step. Build the DOM with `h()` and text nodes only: never innerHTML (uploaded data can contain HTML); no inline scripts (CSP)
- `sawazi/audit.py` append-only audit log. Every new manual action calls `audit.record(...)` before its `commit()`, never logs secrets
- `sawazi/auth.py` staff login (scrypt passwords, hashed opaque session tokens), roles, institution scoping. Every new endpoint needs `Depends(require(...))`; it returns a `Principal` (staff user or API key). Actions that move money to a member or change access go in `HUMAN_ONLY`
- `scripts/` fictional sample data generator (`make_sample_data.py`, includes an answer key `mpesa_truth.csv`), demo run, dashboard build
- `migrations/` Alembic. Every model change needs a reviewed `alembic revision --autogenerate` migration
  (`tests/test_migrations.py` fails if models and migrations disagree)
- `tests/` pytest (`conftest.make_engine()`: SQLite by default, PostgreSQL with `SAWAZI_TEST_DB_URL`)

## Commands
- Install: `python -m pip install -r requirements.txt`
- Tests: `python -m pytest -q` (must stay green; add tests with every feature). Before merging, also run with
  `SAWAZI_TEST_DB_URL=postgresql+psycopg://...` against a throwaway PostgreSQL: it checks foreign keys, column
  lengths and concurrency that SQLite does not
- Demo: `python scripts/make_sample_data.py && python scripts/run_demo.py && python scripts/build_dashboard.py`
- API: `uvicorn sawazi.api:app --reload` then http://localhost:8000/docs

## Quality bar
- Anything that allocates money (matching, C2B, clearing suspense) runs inside `matching_lock(s, institution_id)`.
- Anything that commits a member's deposits (guarantor acceptance, any channel) goes through
  `guarantors.record_answer`, which locks that member's row first.
- Sawazi never lends: approved loans leave as a hand-over file; the core system disburses.
- After any matching change, run `scripts/run_demo.py` and check the ACCURACY line: `auto_allocated_wrong` must stay 0.
- Sample data is fictional. Never commit real member data. Real data needs ODPC registration and a data processing agreement first.
- Secrets (Daraja, Taifa Mobile keys, callback token) go in environment variables / `.env` (gitignored), never in code.
