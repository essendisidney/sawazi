#!/bin/sh
# Nightly PostgreSQL backup: one compressed dump a day in /backups, the oldest removed after BACKUP_KEEP_DAYS.
# Runs in the "backup" container. Copy /backups off the server too (see docs/DEPLOY.md): a backup on the same
# disk does not survive losing the server.
set -eu
BACKUP_AT="${BACKUP_AT:-01:30}"
KEEP="${BACKUP_KEEP_DAYS:-14}"

dump() {
  f="/backups/sawazi-$(date +%Y-%m-%d_%H%M).dump"
  if pg_dump --format=custom --no-owner --file="$f.part" && mv "$f.part" "$f"; then
    echo "$(date '+%F %T') backup written: $f ($(du -h "$f" | cut -f1))"
  else
    rm -f "$f.part"; echo "$(date '+%F %T') BACKUP FAILED" >&2
  fi
  find /backups -name 'sawazi-*.dump' -mtime +"$KEEP" -delete
}

if [ "${1:-}" = "now" ]; then dump; exit 0; fi

echo "Backups at $BACKUP_AT each day, kept $KEEP days."
while true; do
  if [ "$(date +%H:%M)" = "$BACKUP_AT" ]; then dump; sleep 61; fi
  sleep 20
done
