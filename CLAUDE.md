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
- Phase 1 (current): repayment matching, check-off reconciliation, collections. Core engine DONE.
- Phase 2: loan factory (digital applications, appraisal), digital guarantor network (app + USSD)
- Phase 3: exposure/risk view, board pack, SASRA/CBK return generation, member app
- Phase 4: cross-institution network (guarantee exposure, sector benchmarks), MFI group lending, regional

## Phase 1 — what is left, in this order
1. Staff auth DONE: users, roles, per-institution scoping, per-institution API keys (`sawazi/auth.py`, role matrix in `PERMISSIONS`, human-only actions in `HUMAN_ONLY`)
2. Audit log DONE (`sawazi/audit.py`). When SMS sending lands (item 3), record each send/approval with `audit.record`
3. SMS sending via Africa's Talking (sandbox first), delivery status callbacks, opt-out handling, send only on staff approval
4. M-Pesa Daraja C2B validation/confirmation callbacks for real-time matching (statements stay as fallback)
5. Staff web console: suspense clearing screen, exceptions list, collections queue, upload page, dashboard
6. Configurable allocation rules per institution (penalty -> interest -> principal order, deposit/share splits)
7. Alembic migrations; PostgreSQL in production

## Stack and conventions
- FastAPI + SQLAlchemy 2.0 (typed `Mapped[]` models) + pydantic v2. SQLite locally, PostgreSQL in production via `SAWAZI_DB_URL`.
- Money is ALWAYS integer cents (`*_cents`). Never floats for stored money.
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
- `sawazi/engine/matching.py` member matching, allocation, anomaly flags
- `sawazi/engine/checkoff.py` check-off schedule vs remittance reconciliation
- `sawazi/engine/collections.py` arrears ranking, drafted messages, PAR
- `sawazi/api.py` FastAPI app
- `sawazi/audit.py` append-only audit log. Every new manual action calls `audit.record(...)` before its `commit()`, never logs secrets
- `sawazi/auth.py` staff login (scrypt passwords, hashed opaque session tokens), roles, institution scoping. Every new endpoint needs `Depends(require(...))`; it returns a `Principal` (staff user or API key). Actions that move money to a member or change access go in `HUMAN_ONLY`
- `scripts/` fictional sample data generator (`make_sample_data.py`, includes an answer key `mpesa_truth.csv`), demo run, dashboard build
- `tests/` pytest

## Commands
- Install: `python -m pip install -r requirements.txt`
- Tests: `python -m pytest -q` (must stay green; add tests with every feature)
- Demo: `python scripts/make_sample_data.py && python scripts/run_demo.py && python scripts/build_dashboard.py`
- API: `uvicorn sawazi.api:app --reload` then http://localhost:8000/docs

## Quality bar
- After any matching change, run `scripts/run_demo.py` and check the ACCURACY line: `auto_allocated_wrong` must stay 0.
- Sample data is fictional. Never commit real member data. Real data needs ODPC registration and a data processing agreement first.
- Secrets (Daraja, Africa's Talking keys) go in environment variables / `.env` (gitignored), never in code.
