"""Shared parsing helpers for messy SACCO exports."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal


class BadValue(ValueError):
    """A cell Sawazi cannot read with certainty. The row is rejected; it is never read as zero."""


class BadFile(ValueError):
    """A file Sawazi cannot import safely (missing or clashing columns). Nothing from it is saved."""

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
    "%d/%m/%y",
    "%d-%m-%y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d",
]


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (h or "").strip().lower()).strip("_")


def read_rows(content: bytes | str) -> list[dict[str, str]]:
    """Read CSV text into dicts keyed by normalised header names."""
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    names = [norm_header(h) for h in (reader.fieldnames or []) if h is not None]
    clashes = sorted({n for n in names if n and names.count(n) > 1})
    if clashes:  # e.g. "Amount" and "Amount " in one file: one column would silently win
        raise BadFile(f"the file has two columns called '{clashes[0]}'. Rename or remove one and upload again.")
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


_CURRENCY = re.compile(r"k(?:es|shs?)\.?|/=", re.IGNORECASE)
_PLAIN = re.compile(r"\d+(?:\.\d{1,2})?")


def to_cents(value: str | float | int | None) -> int:
    """Shillings as written in Kenyan exports, to integer cents. A blank cell is 0. Anything that cannot be read
    with certainty raises BadValue: "KSH 1,500.00", "1 500.00", "1,000.00 CR", "(500)" and "500-" are read;
    "1.234,50", "N/A" or "12abc" are refused rather than guessed."""
    if value is None:
        return 0
    if isinstance(value, bool):
        raise BadValue(f"'{value}' is not an amount")
    if isinstance(value, int):
        return value * 100
    if isinstance(value, float):
        return int((Decimal(repr(value)) * 100).to_integral_value())
    raw = str(value).strip()
    s = _CURRENCY.sub("", raw).replace("\u00a0", " ").strip()
    if s in {"", "-", "--"}:
        return 0
    negative = False
    if s[-2:].upper() in {"CR", "DR"}:
        negative, s = s[-2:].upper() == "DR", s[:-2].strip()
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1].strip()
    if s.startswith("-") or s.endswith("-"):
        negative, s = True, s.strip("-").strip()
    s = s.replace(",", "").replace(" ", "")
    if not _PLAIN.fullmatch(s):
        raise BadValue(f"'{raw}' is not an amount Sawazi can read")
    cents = int(Decimal(s) * 100)
    return -cents if negative else cents


def amount_problem(row: dict[str, str], *names: str) -> str | None:
    """Why the first filled amount among these columns cannot be read, or None if it can (or is blank)."""
    try:
        to_cents(pick(row, *names))
    except BadValue as e:
        return str(e)
    return None


def has_column(rows: list[dict[str, str]], *names: str) -> bool:
    return bool(rows) and any(n in rows[0] for n in names)


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
