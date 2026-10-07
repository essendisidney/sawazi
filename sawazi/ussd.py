"""Guarantor consent by USSD, for any phone (no smartphone or data needed).

The guarantor dials the shortcode, sees the guarantee requests sent to their number (at any SACCO on
Sawazi), and accepts or declines. The phone number on a USSD session comes from the mobile network, so it
cannot be forged; accepting also asks for the last 4 digits of the member's ID number, in case someone
else is holding the phone. Accepting goes through the same locked capacity check as the web page.

Protocol: the common Kenyan aggregator format. Each request carries a session id and the whole input so
far, joined by "*" (e.g. "1*1*4321"). Replies start with "CON " (expect more input) or "END " (session
over). Screens stay within 182 characters. The list shown first is saved per session, so "2" always means
the request the guarantor read, even if their list changes before they answer.
"""
from __future__ import annotations

import hmac
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import guarantors
from .auth import utcnow
from .models import Guarantee, Institution, LoanApplication, Member, UssdSession

MAX_LISTED = 5


def _short(name: str, n: int = 18) -> str:
    parts = name.title().split()
    s = f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else name.title()
    return s[:n]


def _amount(cents: int) -> str:
    return f"KES {cents / 100:,.0f}"


def open_requests(s: Session, phone: str) -> list[Guarantee]:
    """Requests sent to this number that can still be answered, oldest first (so menu numbers stay put)."""
    rows = s.scalars(select(Guarantee).join(LoanApplication, Guarantee.application_id == LoanApplication.id)
                     .where(Guarantee.phone == phone, Guarantee.status == "requested",
                            LoanApplication.status.in_(guarantors.OPEN_APPLICATION))
                     .order_by(Guarantee.id))
    return [g for g in rows if not guarantors.is_expired(g)][:MAX_LISTED]


def handle(s: Session, session_id: str, phone: str | None, text: str, ip: str | None = None) -> str:
    """One USSD step."""
    if not phone or not session_id:
        return "END Sorry, we could not read your phone number."
    steps = [x.strip() for x in text.split("*")] if text else []

    if not steps:
        requests = open_requests(s, phone)
        if not requests:
            return "END Sawazi: you have no guarantee requests waiting."
        row = s.scalar(select(UssdSession).where(UssdSession.session_id == session_id))
        if row is None:
            row = UssdSession(session_id=session_id, phone=phone)
            s.add(row)
        row.guarantee_ids, row.created_at = [g.id for g in requests], utcnow()
        s.execute(delete(UssdSession).where(UssdSession.created_at < utcnow() - timedelta(days=1)))  # tidy up
        s.commit()
        lines = [f"{i}. {_short(s.get(Member, s.get(LoanApplication, g.application_id).member_id).name)} "
                 f"{_amount(g.amount_cents)}" for i, g in enumerate(requests, 1)]
        return "CON Guarantee requests:\n" + "\n".join(lines)

    row = s.scalar(select(UssdSession).where(UssdSession.session_id == session_id, UssdSession.phone == phone))
    if row is None:
        return "END Session expired. Dial again to see your requests."
    if not steps[0].isdigit() or not 1 <= int(steps[0]) <= len(row.guarantee_ids):
        return "END Invalid choice. Dial again to see your requests."
    g = s.get(Guarantee, row.guarantee_ids[int(steps[0]) - 1])  # exactly the request shown under this number
    if g.phone != phone or guarantors.effective_status(g) != "requested":
        return "END This request is no longer open. Dial again to see your requests."
    a = s.get(LoanApplication, g.application_id)
    applicant, inst = s.get(Member, a.member_id), s.get(Institution, g.institution_id)

    if len(steps) == 1:
        return (f"CON {_short(applicant.name)} asks you to guarantee {_amount(g.amount_cents)} of a "
                f"{_amount(a.amount_cents)} loan at {inst.name[:24]}. If unpaid, it can come from your deposits.\n"
                "1. Accept\n2. Decline")

    choice = steps[1]
    if choice == "2":
        if len(steps) == 2:
            return f"CON Decline guaranteeing {_short(applicant.name)}'s loan?\n1. Yes, decline\n2. No, go back"
        if steps[2] != "1":
            return "END Nothing was changed. Dial again any time."
        if guarantors.record_answer(s, g.id, "decline", ip, "ussd") == "closed":
            s.rollback()
            return "END This request is no longer open."
        s.commit()
        return "END You declined. Nothing more is needed."

    if choice != "1":
        return "END Invalid choice. Dial again to see your requests."
    me = s.get(Member, g.guarantor_member_id)
    if not me.id_number or len(me.id_number) < 4:
        return "END Please accept at the branch or with the link in your SMS: your ID number is not on record."
    if len(steps) == 2:
        return "CON To confirm, enter the last 4 digits of your ID number:"
    if g.pin_attempts >= guarantors.MAX_PIN_ATTEMPTS:
        return "END Too many wrong tries. Please contact your SACCO."
    if not hmac.compare_digest(steps[2], me.id_number[-4:]):
        g = s.scalar(select(Guarantee).where(Guarantee.id == g.id).with_for_update())
        g.pin_attempts += 1
        s.commit()
        return "END Those digits do not match. Nothing was accepted."
    result = guarantors.record_answer(s, g.id, "accept", ip, "ussd")
    if result == "capacity":
        s.rollback()
        return "END Your deposits no longer cover this amount. Please contact your SACCO."
    if result == "closed":
        s.rollback()
        return "END This request is no longer open."
    s.commit()
    return f"END Accepted. You now guarantee {_amount(g.amount_cents)} for {_short(applicant.name)}. Thank you."
