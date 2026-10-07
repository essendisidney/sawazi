# Sawazi by Pesara

Repayment matching, check-off reconciliation and collections for SACCOs and microfinance institutions.
It sits beside any core banking system and works from the CSV/Excel exports every system can produce.

## What Phase 1 does

| Module | What it solves |
|---|---|
| **Importers** | Members and loans from the core system; M-Pesa paybill statements; bank statements; employer check-off schedules and remittances. Re-importing a file never double-counts. |
| **Matching engine** | Finds the member behind every payment using member number, loan number, ID number, bank narrative, registered phone and (as a suggestion only) payer name. Catches typos that land on *another* valid member number. Anything uncertain goes to suspense with a suggested member and the reason. |
| **Allocation** | Splits each payment by the institution's own rules (which loan first; penalty, interest, principal order; current instalment; share capital and deposit split). Exports a postings file for the core system. |
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
python -m pytest -q                     # 228 tests
uvicorn sawazi.api:app --reload          # API at http://localhost:8000/docs
```

Uses SQLite locally. For production set `SAWAZI_DB_URL=postgresql+psycopg://...` and `SAWAZI_API_KEY`.

## Database and deploying

The schema is managed by Alembic (`migrations/`). The API applies pending migrations when it starts, and refuses
to start on a database created before migrations existed (delete local fictional databases and start again).

Production (least cost: one small VPS plus a small managed PostgreSQL 16, or PostgreSQL on the same VPS):

```bash
export SAWAZI_DB_URL=postgresql+psycopg://sawazi:<password>@<host>:5432/sawazi
python -m pip install -r requirements.txt
alembic upgrade head                                  # on every deploy, before starting the API
uvicorn sawazi.api:app --host 127.0.0.1 --port 8000 --workers 2 --proxy-headers   # behind nginx/Caddy for HTTPS
```

- Money is stored in 64-bit integer cents, so amounts are never capped.
- The audit log is append-only in the database itself: PostgreSQL refuses UPDATE, DELETE and TRUNCATE on `audit_events`.
- Several API workers are safe: matching, real-time M-Pesa and clearing suspense take a per-institution lock
  (a PostgreSQL advisory lock), and loan rows are locked while their balances change.
- After changing `sawazi/models.py`: `alembic revision --autogenerate -m "what changed"`, review the file, commit it.
  A test fails if the models and migrations ever disagree.

Run the test suite on PostgreSQL as well as SQLite (the database is wiped for every test, so use a throwaway one):

```bash
docker run -d --name sawazi-pg-test -e POSTGRES_USER=sawazi -e POSTGRES_PASSWORD=test -e POSTGRES_DB=sawazi_test -p 127.0.0.1:55432:5432 postgres:16-alpine
SAWAZI_TEST_DB_URL=postgresql+psycopg://sawazi:test@127.0.0.1:55432/sawazi_test python -m pytest -q
```

## Staff console

A web console for SACCO staff, served by the API itself at `/console/` (plain HTML, CSS and JavaScript: no build
step, nothing extra to host). Staff log in with their Sawazi account and see only what their role allows.

| Screen | What staff do there |
|---|---|
| Dashboard | Match rate, suspense, PAR, open items, payments by source, real-time M-Pesa awaiting a statement |
| Suspense | See each unmatched payment with its details and the suggested member; search members; clear to a member after a confirmation that names them |
| Exceptions | Check-off short/missing/unidentified lines, possible double payments and other flags; resolve with a reason |
| Collections | Ranked arrears queue with drafted messages; tick reminders and send SMS after confirming; failed sends and recent messages with delivery status |
| Upload | Upload any statement or export, run matching, reconcile check-off for an employer and month |

Try it on fictional data:

```bash
python scripts/make_sample_data.py
python scripts/console_demo.py      # http://localhost:8765/console/ (logins printed; SMS simulated)
```

The console shows uploaded data only as text, loads scripts only from its own files, and is served with a strict
Content-Security-Policy. Login tokens are kept for the browser tab only (`sessionStorage`).

## Loans and guarantors (Phase 2)

Sawazi never lends. It takes in an application, appraises it, collects guarantor consent and records a human
decision; the core system disburses from the hand-over file, and the loan comes back through the loans upload.

1. **Member figures.** The members export (or the "member balances" upload, monthly or from payroll) supplies
   date joined, deposits, share capital, gross and net pay. Unknown is never treated as zero.
2. **Loan products** (admin): limits, rate and method (only to estimate the instalment), deposits multiplier
   (default 3x), months of membership (6), arrears limit (30 days), one-third take-home rule, guarantor cover
   (the amount above the member's own deposits), minimum guarantors, and an amount above which two approvers are needed.
3. **Appraisal** checks each rule and says why in plain words, with the largest amount the member qualifies for.
   A figure Sawazi does not have makes the check "unknown", never a pass. Appraisal never approves.
4. **Guarantors** are asked by SMS with a one-time link (7 days) to a simple page; accepting needs a PIN sent to
   their phone on file. Free capacity (deposits less what they already guarantee) is checked when asked and again,
   under a lock, when they accept. Set `SAWAZI_PUBLIC_URL` to the https address members open links on.
5. **Approval** by the new `approver` role, never by the person who prepared the application; two approvers above
   the product's threshold; one decline ends it. Approving despite a failed or unknown check needs a written reason
   and is flagged "approved with exceptions".
6. **Hand-over.** Accountants download approved loans for the core system (each loan once). When the disbursed loan
   appears in the next loans upload with the same member and amount, and only one application matches, it is linked.
7. **Release.** When a disbursed loan is repaid (closed in the loans upload, or paid off by a payment Sawazi
   allocates), its guarantors are released and their deposits are free again. Written-off loans keep a balance, so
   their guarantors stay liable. The exposure view also shows each guarantor's share of what is still owed.

**USSD consent** works on any phone. The guarantor dials the shortcode, picks a request, and accepts by entering the
last 4 digits of their ID number (the phone number itself comes from the network). The list a guarantor saw is saved
per session, so an answer always lands on the request they read. It follows Taifa Mobile's USSD gateway
([documentation](https://ussdbeta.taifamobile.co.ke/documentation)): GET or POST (JSON, form or multipart) with
`MSISDN`, `SESSION_ID`, `SERVICE_CODE`, `USSD_STRING`; replies are plain text starting `CON` or `END`. Settings:
- `SAWAZI_USSD_CALLBACK_TOKEN`: register `https://<host>/callbacks/ussd/<token>` as the service's callback URL
- `SAWAZI_USSD_SHORTCUT`: on a shared code such as `*252*100#`, the routing shortcut (`100`) that starts every
  `USSD_STRING`; leave unset on a dedicated code
- `SAWAZI_USSD_CODE`: the code members dial (e.g. `*252*100#`), so guarantor SMS mention it
- `SAWAZI_USSD_ALLOWED_IPS` (optional): Taifa's gateway addresses, once they confirm them
Try it in Taifa's USSD Sandbox Simulator first. Taifa bills once per session and blocks sessions when the USSD
wallet is empty, so keep it topped up.

All of it is in the console (Loans, Products) and the audit log.

## Allocation rules

Each institution chooses how a matched payment is split (`GET/PUT /institutions/{id}/allocation-rules`, admin only,
or the console's Allocation rules page):

1. **Which loan first:** most overdue (default), oldest loan, or largest arrears. A payment that names a loan always serves that loan first.
2. **Arrears order on each loan:** any order of penalty, interest and principal (default penalty, interest, principal).
3. **Current instalment:** pay it next, or not.
4. **What is left:** all to deposits (default), or a percentage or fixed amount to share capital (or deposits) first and the rest to the other.

Sawazi never calculates interest or penalties. Add `Penalty Arrears` and `Interest Arrears` columns to the loans
export and Sawazi uses them; the rest of arrears is principal. Loans exported without them are paid as one
`loan_arrears` amount, exactly as before. The defaults reproduce Sawazi's original split line for line.
Try rules before saving with `POST /institutions/{id}/allocation-rules/preview` (changes nothing). Rule changes apply
to new allocations only, and are in the audit log. Postings use the targets `loan_penalty`, `loan_interest`,
`loan_principal`, `loan_arrears`, `loan_installment`, `shares` and `deposits`.


## Staff login and roles

Every staff user belongs to exactly one institution and only ever sees that institution's data
(another institution's data returns 404). Log in with `POST /auth/login`, then send
`Authorization: Bearer <token>`. Sessions last 12 hours; logout, password change and deactivation take effect immediately.

| Role | Can |
|---|---|
| viewer | dashboard, exceptions list, reminders |
| credit_officer | viewer + build the collections queue |
| accountant | credit_officer + imports, matching, check-off reconcile, clear suspense / resolve exceptions, postings export |
| admin | accountant + manage staff users |

**API keys for machine access** (a nightly core banking sync, scheduled uploads): an institution admin creates
them with `POST /institutions/{id}/api-keys` and a role of `accountant`, `credit_officer` or `viewer` (never admin).
The full key (`swz_...`) is shown once; only its hash is stored. Send it as `Authorization: Bearer swz_...`.
A key belongs to the institution, not the person who made it, so it keeps working if that person leaves; revoke it with
`DELETE /institutions/{id}/api-keys/{key_id}`. Keys can never clear suspense, resolve exceptions or manage staff or keys:
a person must do those.

## Real-time M-Pesa (Daraja C2B)

Payments to the paybill arrive the moment the member pays, and are matched straight away. Statements stay the
fallback and the source of truth.

- **Sawazi never blocks money.** The validation callback always accepts, and URLs are registered with
  `ResponseType: Completed`, so a payment goes through even if Sawazi is down.
- **Same payment, one record.** A callback's `TransID` is the statement's receipt number, so uploading the statement
  later never double-counts, and repeat callbacks are ignored.
- **The statement confirms every callback.** Safaricom does not sign callbacks, so on each statement upload Sawazi
  marks matching callbacks confirmed and fills in the payer's phone where Safaricom masked it. A different amount
  raises a high-severity `c2b_mismatch` exception. `GET /institutions/{id}/c2b/unconfirmed` lists callbacks not yet
  on any statement (after 48 hours by default).
- **Protection:** a secret in the callback URL (`SAWAZI_DARAJA_CALLBACK_TOKEN`; generate it with
  `python -c "import secrets; print(secrets.token_hex(24))"`), and optionally `SAWAZI_DARAJA_ALLOWED_IPS`
  (comma-separated; take Safaricom's current list from the Daraja portal). Callbacks are closed if the token is not set.
- The institution's `paybill` in Sawazi must equal the shortcode, or callbacks are acknowledged but not recorded.
- Matching is serialised per institution across all API workers, so a payment is never allocated twice.

Register the URLs once per paybill (sandbox first; credentials from that institution's Daraja app):

```bash
SAWAZI_DARAJA_CONSUMER_KEY=... SAWAZI_DARAJA_CONSUMER_SECRET=... SAWAZI_DARAJA_CALLBACK_TOKEN=... \
  python scripts/daraja_register.py register --shortcode 600000 --host https://your-sawazi-host
python scripts/daraja_register.py simulate --shortcode 600000 --amount 100 --account UT00104   # sandbox only
```

## SMS to members (Taifa Mobile)

Nothing goes to a member unless a staff member (credit officer, accountant or admin) approves it:
`POST /institutions/{id}/reminders/send` with the reminder ids from `GET /institutions/{id}/reminders`.
API keys can never send. Before each message Sawazi checks that the number has not opted out, that the member has
a valid phone, that the reminder is a member message (recovery notes never go out), and that the same loan has not
had an SMS in the last 3 days. Each reminder is claimed before sending, so a double click cannot send twice.
If Taifa Mobile does not answer in time the message is marked `unknown` and is never resent automatically.

Providers, set with `SAWAZI_SMS_PROVIDER`:
- `simulate` (default): sends nothing and marks messages `simulated`. Taifa Mobile has no sandbox, so use this to try the flow.
- `taifa`: real sends. Needs `SAWAZI_TAIFA_API_KEY`, and the institution's admin must switch SMS on with
  `PUT /institutions/{id}/sms/settings` (`enabled`, Taifa `service_name`, optional `opt_out_text` added to every message).

Delivery reports and opt-outs come back from Taifa Mobile on callback URLs that carry a secret
(`SAWAZI_SMS_CALLBACK_TOKEN`; the callbacks are closed if it is not set). Register these with Taifa Mobile:

```
https://<your-host>/callbacks/taifa/<token>/delivery
https://<your-host>/callbacks/taifa/<token>/subscription
https://<your-host>/callbacks/taifa/<token>/incoming
```

A member is opted out when they reply STOP, ACHA, SITISHA or UNSUBSCRIBE, unsubscribe from the service, or block the
sender ID; staff can also record an opt-out when a member asks in person. Only an admin can opt a number back in,
with a reason. Every approval, opt-out, opt-in and settings change is in the audit log.

## Audit log

Every manual action is recorded with who did it (staff user, API key, or the Pesara platform), when, and the
state before and after: clearing suspense (with the allocations made), resolving exceptions, imports, matching and
check-off runs, building the collections queue, postings exports, staff and API key changes, logins (with IP),
failed logins against a known account, logouts and password changes. The event is written in the same database
transaction as the change, so there is never a change without its record. Passwords, hashes and keys are never logged.

Admins read it with `GET /institutions/{id}/audit` (newest first; filter by `action` or an action prefix like `user.`,
`entity_type`/`entity_id`, `actor_kind`/`actor_id`, `since`/`until`; page with `before_id`). There is no endpoint to
change or delete an event, and the app refuses updates and deletes. In production, also deny the app's database role
UPDATE and DELETE on `audit_events`.

`SAWAZI_API_KEY` is the Pesara platform key (header `X-API-Key`). It is only for creating institutions and
each institution's first admin, and is never given to an institution. If it is not set those endpoints are closed.

```bash
curl -X POST localhost:8000/institutions -H "X-API-Key: $SAWAZI_API_KEY" -H "Content-Type: application/json" -d '{"name":"Ufanisi SACCO"}'
curl -X POST localhost:8000/institutions/1/admin -H "X-API-Key: $SAWAZI_API_KEY" -H "Content-Type: application/json"      -d '{"email":"admin@ufanisi.co.ke","name":"Admin","role":"admin","password":"at-least-10-chars"}'
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/institutions` | Create a SACCO/MFI tenant (platform key) |
| POST | `/institutions/{id}/admin` | Create the institution's first admin (platform key) |
| POST | `/auth/login`, `/auth/logout`, `/auth/password`; GET `/auth/me` | Staff login, logout, change own password, who am I |
| GET/POST | `/institutions/{id}/users` | List / add staff (admin) |
| PATCH | `/institutions/{id}/users/{user_id}` | Change role, deactivate, reset password (admin) |
| GET/POST | `/institutions/{id}/api-keys` | List / create institution API keys (admin; full key shown once) |
| DELETE | `/institutions/{id}/api-keys/{key_id}` | Revoke an API key (admin) |
| GET | `/institutions/{id}/audit` | Audit log of every manual action, newest first (admin) |
| GET/POST/PUT | `/institutions/{id}/loan-products` | Loan products and their appraisal rules (admin changes) |
| POST | `/institutions/{id}/appraisal/what-if` | Appraise a possible loan for a member, saving nothing |
| POST/GET | `/institutions/{id}/loan-applications` | Create (draft) / list applications |
| PATCH, POST `submit`, `withdraw` | `/institutions/{id}/loan-applications/{app}` | Edit a draft, submit for decision, withdraw |
| POST | `/institutions/{id}/loan-applications/{app}/decide` | Approver's decision (approve needs `override_reason` if the appraisal does not pass) |
| POST | `/institutions/{id}/loan-applications/export.csv` | Approved loans for the core system to disburse |
| POST/GET | `/institutions/{id}/loan-applications/{app}/guarantors` | Ask a guarantor by SMS / list answers |
| GET | `/institutions/{id}/members/{member_no}/guarantor-exposure` | What a member guarantees and can still guarantee |
| GET/PUT | `/institutions/{id}/allocation-rules` | How payments are split (admin changes) |
| POST | `/institutions/{id}/allocation-rules/preview` | Show how a member's payment would be split, changing nothing |
| GET | `/institutions/{id}/members?q=` | Find a member by number, name, phone or ID number, with active loans |
| GET | `/institutions/{id}/c2b/unconfirmed` | Real-time M-Pesa payments not yet seen on a paybill statement |
| POST | `/institutions/{id}/reminders/send` | Approve and send reminders to members by SMS (staff only) |
| GET | `/institutions/{id}/sms` | Messages sent, with delivery status |
| GET/POST | `/institutions/{id}/sms/opt-outs` | Numbers that must not get SMS / record a member's opt-out |
| DELETE | `/institutions/{id}/sms/opt-outs/{phone}?note=` | Opt a number back in (admin, reason required) |
| GET/PUT | `/institutions/{id}/sms/settings` | Switch SMS on, Taifa service name, opt-out text (admin) |
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
  auth.py              staff login, roles, institution scoping, API keys
  audit.py             append-only audit log of every manual action
  sms.py               SMS providers: Taifa Mobile client, simulator
  guarantors.py        guarantor capacity, one-time links, PINs, consent page, release on repayment
  ussd.py              guarantor consent by USSD
  engine/appraisal.py  loan appraisal rules (pure)
  daraja.py            M-Pesa Daraja C2B callback parsing, URL registration
  console/             staff web console (static HTML/CSS/JS served at /console/)
  importers/           CSV parsing for every source
  engine/matching.py   member matching, anomaly flags
  engine/allocation.py per-institution allocation rules (pure planner + apply)
  engine/checkoff.py   check-off reconciliation
  engine/collections.py arrears ranking, messages, PAR
  api.py               FastAPI app
scripts/               sample data generator and demo run
brand/                 logo: mark, lockup, one-colour version, preview sheet
tests/                 pytest suite
```

## Next

Phase 2 is complete (go-live of USSD waits on a Taifa Mobile shortcode). Phase 3: exposure and risk view, board pack, SASRA/CBK returns, member app.

Before any real member data: ODPC registration and a data processing agreement with each pilot institution.

Everything to settle before a pilot, with owners and how to check each item: [docs/PILOT_CHECKLIST.md](docs/PILOT_CHECKLIST.md).
