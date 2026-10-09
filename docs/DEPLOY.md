# Deploying Sawazi

One small Linux server runs everything: PostgreSQL, the Sawazi app (API, staff console, member app), Caddy for
HTTPS, and a nightly backup. Scale up only when pilots need it.

Before any real member data: ODPC registration and a data processing agreement with the SACCO (pilot checklist
item 0). Where the server is hosted, in Kenya or abroad, matters under the Data Protection Act: settle it in that
registration and agreement before choosing a provider.

## What you need

- A Linux server (Ubuntu 24.04 LTS is fine) with 2 vCPU, 2 to 4 GB RAM, 40 GB disk. Ports 80 and 443 open; SSH
  for you only.
- A domain name for it, e.g. `sawazi.ufanisi.co.ke`, with a DNS A record pointing at the server.
- Docker with the compose plugin: `curl -fsSL https://get.docker.com | sh`.
- Somewhere off the server for backup copies (another server, or object storage).

## First deploy

```bash
git clone https://github.com/essendisidney/sawazi.git && cd sawazi/deploy
cp .env.example .env
nano .env            # SAWAZI_DOMAIN, POSTGRES_PASSWORD, SAWAZI_API_KEY (each secret: openssl rand -hex 32)
chmod 600 .env
docker compose up -d --build
docker compose ps    # app and db "healthy" within a minute
curl https://<your domain>/healthz     # ok
```

Caddy fetches the HTTPS certificate on the first request; that needs the DNS record in place and ports 80 and
443 open. Database changes (migrations) are applied automatically when the app starts.

## Set up the SACCO

```bash
docker compose exec app python -m sawazi.setup_institution
```

It asks for the SACCO's name and paybill, and the first admin's name, email and password (not shown as typed). The
admin then logs in at `https://<domain>/console/` and adds staff under **Admin > Staff**.

When that is done, take the platform key off the server: delete the value of `SAWAZI_API_KEY` in `.env` and run
`docker compose up -d`. Put it back only to set up another institution.

## Connect M-Pesa, SMS and USSD

Each comes with its own pilot checklist item; do them in a test window first.
- **SMS (Taifa Mobile):** set `SAWAZI_TAIFA_API_KEY`, then `SAWAZI_SMS_PROVIDER=taifa`. Register these with Taifa:
  `https://<domain>/callbacks/taifa/<SAWAZI_SMS_CALLBACK_TOKEN>/delivery`, `.../subscription` and `.../incoming`.
- **M-Pesa C2B (Daraja):** set `SAWAZI_DARAJA_CALLBACK_TOKEN`, then register
  `https://<domain>/callbacks/c2b/<token>/confirmation` and `/validation` for the paybill, from a checkout of
  this repository with the SACCO's Daraja app keys in your environment (never in `.env` on the server):
  `python scripts/daraja_register.py register --shortcode <paybill> --host https://<domain>`. The paybill set up
  in Sawazi must equal the shortcode. Add Safaricom's addresses to `SAWAZI_DARAJA_ALLOWED_IPS` when they give them.
- **USSD (Taifa):** give Taifa `https://<domain>/callbacks/ussd/<SAWAZI_USSD_CALLBACK_TOKEN>`, and set `SAWAZI_USSD_CODE`,
  `SAWAZI_USSD_ALLOWED_IPS`.

After any change to `.env`: `docker compose up -d`.

**Member app and SMS:** while `SAWAZI_SMS_PROVIDER=simulate`, no SMS leaves the server and the text, including
member sign-in codes, is written to the app log. Anyone who can read that log could sign in as a member, so do not
tell members about the app until real SMS is on.

## Backups

The `backup` container writes one compressed dump a day at 01:30 Nairobi time to `deploy/backups/` and keeps 14
days (`BACKUP_AT`, `BACKUP_KEEP_DAYS` in `.env`). A backup on the same disk does not survive losing the server:
copy the folder off it every day, for example with a cron job using `rsync` or `rclone`.

Take one now: `docker compose exec backup sh /backup.sh now`

**Test a restore every month**, into a scratch database, and compare the counts:

```bash
F=$(ls backups | tail -1)
docker compose exec db psql -U sawazi -d postgres -c "create database restore_check"
docker compose exec backup pg_restore --no-owner -d restore_check /backups/$F
for d in sawazi restore_check; do
  docker compose exec db psql -U sawazi -d $d -tAc "select count(*), sum(amount_cents) from allocations"
done
docker compose exec db psql -U sawazi -d postgres -c "drop database restore_check"
```

**Restoring for real** (after losing the database): stop the app, recreate the database, restore, start.

```bash
docker compose stop app
docker compose exec db psql -U sawazi -d postgres -c "drop database sawazi" -c "create database sawazi"
docker compose exec backup pg_restore --no-owner -d sawazi /backups/<file>.dump
docker compose start app
```

## Updates

```bash
cd sawazi && git pull && cd deploy
docker compose exec backup sh /backup.sh now     # a backup just before
docker compose up -d --build
docker compose ps && curl https://<domain>/healthz
```

To go back, check out the previous version and run `docker compose up -d --build` again. A version that changed the
database cannot always be undone that way; restore the backup taken before the update instead.

## Day to day

- Logs: `docker compose logs -f app` (and `caddy`, `db`, `backup`).
- Health: point an uptime monitor at `https://<domain>/healthz` (it answers `ok`, or 503 if the database is down).
- Disk: `df -h` and `du -sh backups`; PostgreSQL and backups are the only things that grow.
- Server updates: `sudo apt update && sudo apt upgrade` monthly; reboots are safe, everything restarts itself.

## How it fits together

| Container | Does | Reachable from |
|---|---|---|
| caddy | HTTPS, certificates, security headers, passes the caller's real address on | the internet (80, 443) |
| app | API, `/console/`, `/app/`; runs migrations at start; 2 workers | caddy only |
| db | PostgreSQL 16, data in the `pgdata` volume | app and backup only |
| backup | nightly `pg_dump` to `deploy/backups/` | nothing |

The app trusts the caller address Caddy forwards, because nothing else can reach it; Caddy replaces any address a
caller tries to supply. The Daraja and USSD allow-lists depend on this. Never publish the app's port 8000 directly.

## Try it on your own computer

Set `SAWAZI_DOMAIN=localhost` in `.env`, run `docker compose up -d --build`, and open `https://localhost/console/`
(accept the browser warning: Caddy uses a local test certificate). Use the setup command above, and the fictional
files in `sample_data/` for uploads.
