# Deploying on Vercel and Supabase

The managed alternative to the Docker kit in `docs/DEPLOY.md`: Supabase runs PostgreSQL (with daily backups on the
Pro plan), Vercel runs the app (API, staff console, member app) with HTTPS. Nothing to patch or back up by hand.

Before any real member data: ODPC registration and a data processing agreement (pilot checklist item 0). The
Supabase project `sawazi` is in eu-west-1 (Ireland) and the Vercel functions run in Dublin (`dub1`, `vercel.json`):
member data would be held outside Kenya, which the Data Protection Act treats as a transfer needing safeguards.
Settle that in the registration and agreement first.

## How it is wired

| Piece | Where | Notes |
|---|---|---|
| App | Vercel project `sawazi`, connected to GitHub `main` | `pyproject.toml` names the app (`sawazi.api:app`); every push to `main` deploys |
| Region | `dub1` (Dublin) | next to the database; `vercel.json` |
| Database | Supabase project `sawazi`, PostgreSQL 17 | the app connects through the transaction pooler (port 6543) |
| Migrations | run once per release, before it goes live | the app never migrates itself on Vercel; `/healthz` says `database needs migrating` if this was missed |
| Supabase Data API | locked out | after every migration Sawazi's tables get row level security and the `anon` and `authenticated` roles lose all access (`sawazi/db.py`, `SUPABASE_LOCKDOWN`) |
| Caller address | `x-real-ip`, set by Vercel | used for the audit log and the Daraja and USSD allow-lists (`auth.client_ip`) |

## Settings (Vercel > Project > Settings > Environment Variables, Production)

- `SAWAZI_DB_URL`: Supabase > Connect > **Transaction pooler** string, e.g.
  `postgresql://postgres.<ref>:<password>@aws-0-eu-west-1.pooler.supabase.com:6543/postgres`
- `SAWAZI_PUBLIC_URL`: the address members and staff use, e.g. `https://sawazi.vercel.app` or your own domain
- `SAWAZI_API_KEY`: only while setting up an institution (see below); remove it afterwards
- The SMS, Daraja and USSD settings from `deploy/.env.example`, when each is ready (pilot checklist 4 to 6). Each
  provider's callbacks are refused until its addresses are in `SAWAZI_SMS_ALLOWED_IPS`, `SAWAZI_DARAJA_ALLOWED_IPS` or
  `SAWAZI_USSD_ALLOWED_IPS`: the secret in the URL alone is not enough

Never give Preview deployments the production `SAWAZI_DB_URL`: a preview runs unreviewed branches.

## Each release

1. Migrate Supabase from your own computer, through the **session** pooler (port 5432; Alembic needs a normal
   session):
   `SAWAZI_DB_URL="postgresql://postgres.<ref>:<password>@aws-0-eu-west-1.pooler.supabase.com:5432/postgres" alembic upgrade head`
2. Merge to `main`. Vercel builds and deploys it.
3. Check `https://<domain>/healthz` answers `ok`.

If a release has no new migration, step 1 does nothing and can be skipped.

## Set up a SACCO

With `SAWAZI_API_KEY` set in Vercel and in your shell (the same value):

```bash
SAWAZI_SETUP_URL=https://<domain> python -m sawazi.setup_institution
```

Then remove `SAWAZI_API_KEY` from Vercel and redeploy.

## Limits to know

- **Uploads:** Vercel functions accept requests up to 4.5 MB. A month's paybill statement for a few thousand
  members fits; a very large one must be split by date range.
- **Time:** a request may run 60 seconds (`vercel.json`). Matching a month for a few thousand members takes a few
  seconds.
- **Backups:** Supabase Pro keeps daily backups for 7 days (Database > Backups). Once a month, also keep a copy
  outside Supabase: `python scripts/backup_offsite.py D:/sawazi-backups` (needs Docker; asks for the Session pooler
  address without showing it; checks the copy reads back; keeps the newest 12). Keep that folder encrypted and out
  of GitHub, which is public.
- **SMS while simulated:** sign-in codes are written to the function log (Vercel > Logs). Keep the member app quiet
  until real SMS is on.

## Uptime and tests

- `.github/workflows/uptime.yml` checks `/healthz` every 15 minutes; a failed run emails whoever last changed the
  file. GitHub pauses scheduled checks after 60 days with no commits; re-enable it under Actions if so.
- `.github/workflows/tests.yml` runs the whole suite on SQLite and on PostgreSQL 17 for every pull request and every
  push to `main`. Merge only when both are green.

## Checking the lockdown

Supabase > Advisors > Security should show no "RLS disabled" errors and no warnings. One "RLS enabled, no policy" notice per table is expected and intended: no policy means no access through the public API. If a table ever appears
there, run `alembic upgrade head` again: the lockdown runs after every upgrade.
