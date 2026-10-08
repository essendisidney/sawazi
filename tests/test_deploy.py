"""The deployment kit stays in step with the code (docs/DEPLOY.md)."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
SET_BY_COMPOSE = {"SAWAZI_DB_URL", "SAWAZI_PUBLIC_URL"}  # built from other settings in docker-compose.yml
NOT_FOR_SERVER = {"SAWAZI_SETUP_URL", "SAWAZI_TAIFA_URL"}  # internal default / optional override (commented in the example)


def test_every_setting_the_app_reads_is_in_the_example():
    used = set()
    for f in (ROOT / "sawazi").rglob("*.py"):
        used |= set(re.findall(r"""getenv\(\s*["'](SAWAZI_[A-Z_]+)""", f.read_text(encoding="utf-8")))
    example = (DEPLOY / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^#?\s*(SAWAZI_[A-Z_]+)=", example, re.M))
    missing = used - listed - SET_BY_COMPOSE - NOT_FOR_SERVER
    assert not missing, f"add to deploy/.env.example: {sorted(missing)}"
    # no secret values in the example
    for line in example.splitlines():
        if re.match(r"^(POSTGRES_PASSWORD|SAWAZI_[A-Z_]*(KEY|TOKEN))=", line):
            assert line.endswith("="), line


def test_only_the_proxy_is_published():
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    services = re.split(r"\n  (?=\w+:\n)", compose.split("\nservices:\n", 1)[1].split("\nvolumes:\n")[0])
    published = [s.split(":")[0].strip() for s in services if "\n    ports:" in s]
    assert published == ["caddy"]


def test_image_leaves_out_data_and_secrets():
    ignore = [x.strip() for x in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() if x.strip() and not x.startswith("#")]
    assert ignore[0] == "*" and "!sample_data/" not in ignore and not any(".env" in x for x in ignore if x.startswith("!"))
    assert "USER sawazi" in (ROOT / "Dockerfile").read_text(encoding="utf-8")


def test_shell_scripts_keep_unix_line_endings():
    assert b"\r\n" not in (DEPLOY / "backup.sh").read_bytes()
    assert "*.sh text eol=lf" in (ROOT / ".gitattributes").read_text(encoding="utf-8")
