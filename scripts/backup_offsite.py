"""Monthly copy of the live database, kept outside Supabase (docs/DEPLOY_VERCEL.md, "Backups").

    python scripts/backup_offsite.py D:/sawazi-backups

Asks for the database address (Supabase > Connect > Session pooler, port 5432) without showing it, writes a
compressed dump named by date into the folder, checks the dump can be read back, and keeps the newest 12.
Needs Docker (it runs PostgreSQL 17's own pg_dump, matching Supabase). The folder holds member data: keep it
encrypted (BitLocker) and off this repository; never put a backup in GitHub, which is public.
"""
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

IMAGE = "postgres:17-alpine"
KEEP = 12


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    folder = Path(sys.argv[1]).resolve()
    if (Path(__file__).resolve().parent.parent in folder.parents) or folder == Path(__file__).resolve().parent.parent:
        sys.exit("Choose a folder outside the Sawazi repository: backups must never be committed.")
    folder.mkdir(parents=True, exist_ok=True)
    url = os.getenv("SAWAZI_BACKUP_URL") or getpass.getpass("Database address (session pooler, port 5432; not shown): ")
    url = url.strip()
    if not url.startswith(("postgres://", "postgresql://")):
        sys.exit("That is not a database address (postgresql://...).")
    if ":6543/" in url:
        sys.exit("That is the transaction pooler (port 6543). Use Supabase's Session pooler address, port 5432.")
    name = f"sawazi-{date.today():%Y-%m}.dump"
    env = {**os.environ, "SAWAZI_BACKUP_URL": url}
    mount = ["-v", f"{folder}:/backups"]
    dump = subprocess.run(["docker", "run", "--rm", "-e", "SAWAZI_BACKUP_URL", *mount, IMAGE, "sh", "-c",
                           f'pg_dump --format=custom --no-owner --dbname="$SAWAZI_BACKUP_URL" --file=/backups/{name}.part '
                           f"&& mv /backups/{name}.part /backups/{name}"], env=env, capture_output=True, text=True)
    if dump.returncode:
        (folder / f"{name}.part").unlink(missing_ok=True)
        sys.exit("Backup failed: " + dump.stderr.replace(url, "***").strip()[-400:])
    check = subprocess.run(["docker", "run", "--rm", *mount, IMAGE, "pg_restore", "--list", f"/backups/{name}"],
                           capture_output=True, text=True)
    tables = sum(1 for line in check.stdout.splitlines() if " TABLE DATA " in line)
    if check.returncode or tables == 0:
        sys.exit(f"The dump {name} was written but could not be read back. Do not rely on it; try again.")
    size = (folder / name).stat().st_size
    print(f"Backup written: {folder / name} ({size / 1_048_576:.1f} MB, {tables} tables). Readable.")
    old = sorted(folder.glob("sawazi-*.dump"))[:-KEEP]
    for f in old:
        f.unlink()
    if old:
        print(f"Removed {len(old)} older copies; keeping the newest {KEEP}.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Nothing was written.")
