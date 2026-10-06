"""Shared parsing helpers for messy SACCO exports."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime

DATE_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y",
    "%d-%b-%Y",
    "%d %b %Y",
]


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def read_rows(content: bytes | str) -> list[dict[str, str]]:
    """Read CSV text into dicts keyed by normalised header names."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    rows = []
    for raw in reader:
        row = {norm_header(k): (v or "").strip() for k, v in raw.items() if k is not None}
        if any(row.values()):
            rows.append(row)
    return rows


def pick(row: dict[str, str], *names: str) -> str:
    """Return the first non-empty value among candidate column names."""
    for n in names:
        v = row.get(n)
        if v:
            return v
    return ""


def to_cents(value: str | float | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100))
    s = str(value).strip().replace(",", "").replace("KES", "").replace("Ksh", "").strip()
    if not s or s in {"-", "--"}:
        return 0
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        cents = int(round(float(s) * 100))
    except ValueError:
        return 0
    return -cents if negative else cents


def to_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def to_date(value: str) -> date | None:
    dt = to_datetime(value)
    return dt.date() if dt else None


def norm_phone(value: str | None) -> str | None:
    """Normalise Kenyan numbers to 2547XXXXXXXX / 2541XXXXXXXX."""
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif len(digits) == 9 and digits[0] in "17":
        digits = "254" + digits
    if digits.startswith("254") and len(digits) == 12:
        return digits
    return None


def norm_ref(value: str | None) -> str:
    """Normalise account references members type at the paybill (e.g. ' mn-00123 ' -> 'MN00123')."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def norm_name(value: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z ]", " ", (value or "").upper())).strip()
