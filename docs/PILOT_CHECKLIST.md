# Sawazi pilot checklist

Everything to settle before the first SACCO uses Sawazi with real members. None of it is code. Tick an item only
when its "done when" is true.

| # | Item | Owner | Blocks |
|---|------|-------|--------|
| 0 | ODPC registration and a data processing agreement with the pilot SACCO | Pesara (Sidney) | Any real member data |
| 1 | Native-speaker check of the Swahili on the guarantor page and in the member app | Sidney + a native speaker | Guarantor requests, member app |
| 2 | `SAWAZI_PUBLIC_URL` on the live server | Whoever deploys | Guarantor requests |
| 3 | Deposits-multiplier policy confirmed with the pilot SACCO | Sidney + SACCO credit committee | Loan appraisal |
| 4 | Taifa Mobile SMS: number format and API key confirmed, one live test | Sidney (Taifa account) | Real SMS |
| 5 | Taifa Mobile USSD: service code, sandbox run, gateway IPs | Sidney (Taifa account) | USSD consent |
| 6 | Daraja C2B tested in the Safaricom sandbox | Sidney (Daraja app) | Real-time M-Pesa |
| 7 | Core-system guarantee export, and SASRA classification rates checked | Sidney + pilot SACCO | Risk view |
| 8 | SASRA Form 4 and Form 3 templates in the repo; provisioning policy answers | Sidney | Filing returns from Sawazi |
| 9 | Pilot server: hosting chosen, deployed, backups copied off the server, a restore tested | Pesara (Sidney) | Going live |

---

## 0. Data protection (before any real member data)

Sample data is fictional. Real member data needs ODPC registration and a data processing agreement with each pilot
institution first.

**Done when:** both are signed and on file.

## 1. Swahili on the guarantor page and in the member app

The member app's words are in `sawazi/app/app.js` (the `T.sw` block, next to the English). Some messages still
come from the server in English (wrong code, wrong PIN, the loan check's reasons).


The page a guarantor opens from the SMS link (`sawazi/guarantors.py`, `summary()`, and the buttons in
`sawazi/api.py`, `_consent_page`) has one Swahili sentence and English-only buttons.

**Now:**

> Ukikubali, akiba yako inaweza kutumika kulipa deni hili hadi KES 54,460 asipolipa.
> ("If you accept, your savings can be used to pay this debt up to KES 54,460 if they do not pay.")

**Proposed (for the native speaker to correct, not yet live):** this names what the member is agreeing to
(*kumdhamini*, to stand guarantor for) and calls it a loan (*mkopo*), not a debt (*deni*).

> Ukikubali kumdhamini {jina}, akiba yako inaweza kutumika kulipa mkopo huu hadi KES 54,460 iwapo hatalipa.
> ("If you agree to stand guarantor for {name}, your savings can be used to repay this loan up to KES 54,460
> if they do not pay.")

Buttons, shown in both languages:

| English | Proposed Swahili |
|---|---|
| Send me a PIN | Nitumie PIN |
| Accept: I guarantee KES 54,460 | Kubali: Ninadhamini KES 54,460 |
| Decline | Kataa |
| PIN from the SMS | PIN kutoka kwenye SMS |

The USSD screens (`sawazi/ussd.py`) are English only. USSD screens are capped at 182 characters, so Swahili would
need a language choice on the first screen, not both languages on one screen. Decide whether pilots need it.

**Done when:** a native speaker has corrected the wording above, and it is changed in the code.

## 2. Public address for guarantor links

Guarantor SMS contain a link to `SAWAZI_PUBLIC_URL`. It must be the server's real HTTPS address. Sawazi refuses
anything that isn't HTTPS (except `localhost` for development), and never takes the address from the incoming request.

```bash
SAWAZI_PUBLIC_URL=https://sawazi.example.co.ke
```

**Done when:** in simulate mode, asking a guarantor produces an SMS (printed in the server log) whose link opens the
consent page on a phone.

## 3. Deposits-multiplier policy

Today, appraisal adds the member's **other outstanding loan balances to the new loan** and checks the total against
the product's multiplier (default 3x deposits). Example: deposits KES 100,000, existing balance KES 50,000, so the new
loan can be up to KES 250,000.

Some SACCOs check **only the new loan** against the multiplier, or exclude loans being refinanced.

**Done when:** the pilot SACCO's credit policy is confirmed. If it differs, change the deposits check in
`sawazi/engine/appraisal.py`; it's one line, and the tests in `tests/test_appraisal.py` show the expected figures. The
multiplier itself is set per product on the console's Products page.

## 4. Taifa Mobile SMS

Sawazi sends numbers as `2547XXXXXXXX`; Taifa's own example uses `07XXXXXXXX`. Their library uses only the first 16
characters of the API key for encryption.

1. Confirm both with Taifa (see the email below).
2. Set `SAWAZI_SMS_PROVIDER=taifa`, `SAWAZI_TAIFA_API_KEY`, `SAWAZI_SMS_CALLBACK_TOKEN`.
3. Register the delivery, subscription and incoming callback URLs with Taifa (see the README).
4. Switch SMS on for the institution: console or `PUT /institutions/{id}/sms/settings`.
5. Approve **one** reminder to your own number.

**Done when:** that SMS arrives, and its delivery report shows `delivered` in the console's Recent messages.

## 5. Taifa Mobile USSD

1. Get a USSD service code. On a shared code (e.g. `*252*100#`), note the shortcut (`100`).
2. Set `SAWAZI_USSD_CALLBACK_TOKEN`, `SAWAZI_USSD_CODE`, and `SAWAZI_USSD_SHORTCUT` (shared codes only).
3. Register `https://<host>/callbacks/ussd/<SAWAZI_USSD_CALLBACK_TOKEN>` as the service's callback URL.
4. Run the whole guarantor flow in Taifa's USSD Sandbox Simulator: list, choose, accept with ID digits, decline.
   This also shows what `USSD_STRING` holds on a session's first request; Sawazi handles both an empty string and
   the shortcut alone.
5. Ask Taifa for their gateway IP addresses and set `SAWAZI_USSD_ALLOWED_IPS`.
6. Top up the USSD wallet. Taifa blocks sessions at zero balance and bills once per session.

**Done when:** a guarantee is accepted end to end in the simulator and shows "accepted, via ussd" in the audit log.

## 6. Daraja C2B in the Safaricom sandbox

With the sandbox app's credentials from the Daraja portal:

```bash
export SAWAZI_DARAJA_ENV=sandbox
export SAWAZI_DARAJA_CONSUMER_KEY=...
export SAWAZI_DARAJA_CONSUMER_SECRET=...
export SAWAZI_DARAJA_CALLBACK_TOKEN=$(python -c "import secrets; print(secrets.token_hex(24))")
python scripts/daraja_register.py register --shortcode 600000 --host https://<your-host>
python scripts/daraja_register.py simulate --shortcode 600000 --amount 100 --account UT00104
```

The institution's paybill in Sawazi must equal the shortcode, or callbacks are acknowledged but not recorded.

**Done when:**
- the simulated payment appears in Sawazi and is matched (or sits in suspense with a reason);
- you've noted whether `MSISDN` arrived plain, masked or hashed. If hashed, consider matching on the hash of
  member phones (CLAUDE.md, Phase 1 item 4).

---

## Every setting

| Setting | What it is |
|---|---|
| `SAWAZI_DB_URL` | `postgresql+psycopg://...` in production |
| `SAWAZI_API_KEY` | Pesara platform key: create institutions and their first admin |
| `SAWAZI_PUBLIC_URL` | HTTPS address members open guarantor links on |
| `SAWAZI_SMS_PROVIDER` | `simulate` (default) or `taifa` |
| `SAWAZI_TAIFA_API_KEY`, `SAWAZI_TAIFA_URL` | Taifa Mobile SMS key (URL has a default) |
| `SAWAZI_SMS_CALLBACK_TOKEN` | Secret in Taifa's SMS callback URLs |
| `SAWAZI_DARAJA_CALLBACK_TOKEN`, `SAWAZI_DARAJA_ALLOWED_IPS` | Daraja C2B callbacks: secret URL and optional IP allowlist |
| `SAWAZI_DARAJA_ENV`, `SAWAZI_DARAJA_CONSUMER_KEY`, `SAWAZI_DARAJA_CONSUMER_SECRET` | For `scripts/daraja_register.py` only |
| `SAWAZI_USSD_CALLBACK_TOKEN`, `SAWAZI_USSD_ALLOWED_IPS` | Taifa USSD callback: secret URL and optional IP allowlist |
| `SAWAZI_USSD_SHORTCUT` | Shared USSD code only: the routing shortcut (e.g. `100`) |
| `SAWAZI_USSD_CODE` | The code members dial, mentioned in guarantor SMS |

Secrets go in environment variables or `.env` (gitignored), never in code.

---

## Draft email to Taifa Mobile

> **Subject:** Sawazi integration: SMS number format, USSD service code and gateway IPs
>
> Hello,
>
> We are integrating Sawazi (by Pesara) with Taifa Mobile for SMS and USSD, for SACCOs in Kenya. Before going live,
> could you confirm the following?
>
> **SMS (clientapi/sms)**
> 1. Recipient format: we send `2547XXXXXXXX`. Your library example uses `07XXXXXXXX`. Are both accepted?
> 2. API key: your PHP library uses the first 16 characters of the API key for the AES-128 encryption. Is that how
>    your server reads it, whatever the key's length?
> 3. Can the delivery report, subscription and incoming message callback URLs be set per account, or per service?
>
> **USSD**
> 4. We would like a USSD service code for guarantor consent. Is a dedicated code available, or a shared code with a
>    shortcut, and what are the costs and lead time?
> 5. On the first request of a session, what does `USSD_STRING` contain on a shared code: empty, or the shortcut?
> 6. Which IP addresses will your USSD and SMS gateways call our callback URLs from, so we can allow only those?
> 7. Is there a timeout for our reply to each USSD request?
>
> Thank you,
> Sidney Essendi, Pesara

## 7. Guarantees from the core system, and classification rates

The risk view and guarantor capacity are only as complete as the guarantee list. Export every active guarantee from
the core system (Loan No, Guarantor Member No, Amount Guaranteed) and upload it as "Guarantees from the core system",
ticking "complete list" when it is the whole book.

The loan classes and provision rates in `sawazi/engine/exposure.py` (`CLASSES`: 1%, 5%, 25%, 50%, 100% for
performing, watch 1-30 days, substandard 31-180, doubtful 181-360, loss over 360) must match the current SASRA form
before the provision figure is used for anything official.

**Done when:** the pilot SACCO's guarantee export uploads with no rejected rows, and the classes and rates are confirmed.

## 8. SASRA returns

Sawazi has a Form 4 working schedule (console, Returns). To fill SASRA's forms in their exact layout it needs:

1. The current **Form 4** (risk classification of assets and provisioning) Excel template, and **Form 3** (deposit
   return) if wanted, from SASRA's website or a SACCO's last filing, saved in the repository.
2. Answers from the pilot SACCO's credit policy: are deposits held against a loan netted off before provisioning,
   and do rescheduled loans keep their earlier classification?

**Done when:** Sawazi's output, pasted into the official template, matches a return the SACCO filed by hand.

## 9. Pilot server

Follow `docs/DEPLOY.md`: one small server running the Docker kit in `deploy/` (PostgreSQL, the app, HTTPS, nightly
backups). Choose where it is hosted together with item 0: the Data Protection Act treats hosting outside Kenya as
a transfer that needs its own safeguards. Keep the member app quiet until real SMS is on (item 4): in simulate mode
sign-in codes are written to the server log.

**Done when:** the SACCO's domain answers `ok` at `/healthz` over HTTPS, the platform key is off the server,
backups are copied off the server every day, and a restore into a scratch database matches the live counts.
