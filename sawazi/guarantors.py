"""Digital guarantor consent: capacity, one-time links, PINs and the consent page.

A guarantor is asked by SMS with a one-time link to a plain page (no app, no login). Accepting needs a PIN
sent by SMS to the guarantor's phone on file, so the person holding that phone is the one agreeing.
Only hashes of the link token and the PIN are stored. Capacity is the guarantor's deposits minus what they
already guarantee, checked when asked and again, under a lock, at the moment they accept.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .auth import utcnow
from .models import Guarantee, Institution, LoanApplication, LoanProduct, Member

LINK_DAYS = 7
PIN_MINUTES = 10
MAX_PIN_ATTEMPTS = 5
MAX_PINS = 3
OPEN_APPLICATION = ("draft", "submitted")


def kes(cents: int) -> str:
    return f"KES {cents / 100:,.0f}" if cents % 100 == 0 else f"KES {cents / 100:,.2f}"


def public_url() -> str | None:
    """Where members open links. Configured, never taken from the request (a forged Host header must not
    be able to make Sawazi text members a link to someone else's site)."""
    url = os.getenv("SAWAZI_PUBLIC_URL", "").rstrip("/")
    return url if url.startswith("https://") or url.startswith("http://localhost") else None


def new_token() -> str:
    return secrets.token_urlsafe(12)  # 16 characters: short enough for one SMS


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def pin_hash(g: Guarantee, pin: str) -> str:
    return hashlib.sha256(f"{g.id}:{g.token_hash}:{pin}".encode()).hexdigest()


# ---------------------------------------------------------------- capacity

def pledged_cents(s: Session, institution_id: int, member_id: int) -> int:
    return s.scalar(select(func.coalesce(func.sum(Guarantee.amount_cents), 0)).where(
        Guarantee.institution_id == institution_id, Guarantee.guarantor_member_id == member_id,
        Guarantee.status == "accepted")) or 0


def free_capacity(s: Session, m: Member) -> int | None:
    if m.deposits_cents is None:
        return None
    return m.deposits_cents - pledged_cents(s, m.institution_id, m.id)


def is_expired(g: Guarantee) -> bool:
    return g.status == "requested" and g.expires_at <= utcnow()


def effective_status(g: Guarantee) -> str:
    return "expired" if is_expired(g) else g.status


# ---------------------------------------------------------------- messages

def request_sms(inst: Institution, applicant: Member, a: LoanApplication, g: Guarantee, link: str) -> str:
    first = applicant.name.split()[0].title()
    return (f"{inst.name}: {applicant.name.title()} ({applicant.member_no}) asks you to guarantee "
            f"{kes(g.amount_cents)} of their {kes(a.amount_cents)} loan. Open {link} to accept or decline. "
            f"Only accept if you know {first} asked.")


def pin_sms(inst: Institution, applicant: Member, pin: str) -> str:
    return (f"{inst.name}: your PIN to accept guaranteeing {applicant.name.title()}'s loan is {pin}. "
            f"It expires in {PIN_MINUTES} minutes. Never share it, not even with SACCO staff.")


def redact(text: str, token: str) -> str:
    return text.replace(token, "[link]")


# ---------------------------------------------------------------- the consent page (server-rendered, no JS)

PAGE_CSP = ("default-src 'none'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


def page(title: str, *body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">
<title>{html.escape(title)} · Sawazi</title>
<link rel="icon" href="/console/logo.svg" type="image/svg+xml">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<link rel="stylesheet" href="/console/console.css"></head>
<body><div class="login"><div class="panel stack">
<div class="login-brand"><img src="/console/logo.svg" alt="" width="40" height="40">
<div><div class="mark">sawazi</div><div class="by">GUARANTOR CONSENT</div></div></div>
{''.join(body)}
</div></div></body></html>"""


def esc(v) -> str:
    return html.escape(str(v), quote=True)


def summary(inst: Institution, applicant: Member, a: LoanApplication, prod: LoanProduct, g: Guarantee) -> str:
    return f"""<h1>{esc(inst.name)}</h1>
<p><b>{esc(applicant.name.title())}</b> (member {esc(applicant.member_no)}) has asked you to guarantee part of a loan.</p>
<dl class="kv">
<div><dt>Loan</dt><dd>{esc(prod.name)}, {esc(kes(a.amount_cents))} over {a.term_months} months</dd></div>
<div><dt>You would guarantee</dt><dd><b>{esc(kes(g.amount_cents))}</b></dd></div>
</dl>
<p class="muted small">If {esc(applicant.name.split()[0].title())} does not repay, the SACCO can recover up to
{esc(kes(g.amount_cents))} from your deposits. Only accept if you know them and agreed to this.
<br><i>Ukikubali, akiba yako inaweza kutumika kulipa deni hili hadi {esc(kes(g.amount_cents))} asipolipa.</i></p>"""
