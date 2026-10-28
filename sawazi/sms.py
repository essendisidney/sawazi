"""Sending SMS to members, through Taifa Mobile.

Nothing is ever sent without a staff member approving it (see the /send endpoint).

Providers
- simulate (default): sends nothing, marks messages `simulated`. Taifa Mobile has no
  sandbox, so this is how to try the whole flow safely.
- taifa: real sends. Needs SAWAZI_SMS_PROVIDER=taifa, TAIFA_API_KEY (or SAWAZI_TAIFA_API_KEY),
  and the institution's SMS settings switched on.

Taifa Mobile API v2: POST https://api.taifamobile.co.ke/v2/sms/sendsms with an `h_api_key`
header and JSON {"mobile", "sender_name", "message", "response_type": "json"}. Always HTTP 200;
the reply is [{"status_code", "status_desc", "message_id", ...}] (or a bare object), and
status_code "1000" means accepted. (The old encrypted sms.taifamobile.co.ke/clientapi is gone.)
The sender is the institution's service name, else TAIFA_SENDER_ID, else SIDNET.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass

import httpx

TAIFA_URL = "https://api.taifamobile.co.ke/v2/sms/sendsms"
TAIFA_OK = "1000"
DEFAULT_SENDER = "SIDNET"  # the only sender approved on the Taifa account

# Delivery report statuses. Anything not "delivered" is kept verbatim for staff to read.
DELIVERED = {"DeliveredToTerminal"}
SENDER_BLOCKED = "sender_ID blacklisted by user"  # the member blocked us: treat as an opt-out

STOP_WORDS = {"STOP", "UNSUBSCRIBE", "ACHA", "SITISHA"}


@dataclass
class SendResult:
    status: str  # sent | failed | unknown | simulated
    provider_message_id: str | None = None
    provider_status: str | None = None
    description: str | None = None


class SimulatedProvider:
    name = "simulate"

    def __init__(self):
        self.sent: list[tuple[str, str, str | None]] = []

    def send(self, phone: str, text: str, service_name: str | None) -> SendResult:
        self.sent.append((phone, text, service_name))
        # Local development only: show what would have gone out (e.g. a guarantor link) in the server log.
        print(f"[simulated SMS to {phone}] {text}", flush=True)
        return SendResult("simulated", f"sim-{uuid.uuid4().hex}", None, "Simulated: nothing was sent")


def _unquote(value: str | None) -> str:
    return (value or "").strip().strip("\"'").strip()


def taifa_reply(text: str) -> tuple[dict, str | None]:
    """(first row of the reply, None if accepted else why not)."""
    try:
        data = json.loads(text)
    except ValueError:
        return {}, f"Bad response from Taifa: {text[:120]}"
    row = (data[0] if data else {}) if isinstance(data, list) else data
    if not isinstance(row, dict):
        row = {}
    code = "" if row.get("status_code") is None else str(row["status_code"])
    if code == TAIFA_OK:
        return row, None
    return row, f"Taifa {code or '?'}: {row.get('status_desc') or text[:120]}"


def taifa_rejection(text: str) -> str | None:
    """None when Taifa accepted the message (status_code "1000"), else the reason."""
    return taifa_reply(text)[1]


class TaifaProvider:
    name = "taifa"

    def __init__(self, api_key: str, base_url: str = TAIFA_URL, transport: httpx.BaseTransport | None = None,
                 sender: str | None = None):
        self.api_key = _unquote(api_key)
        self.url = base_url
        self.sender = _unquote(sender) or DEFAULT_SENDER
        self.client = httpx.Client(timeout=20, transport=transport)

    def send(self, phone: str, text: str, service_name: str | None) -> SendResult:
        body = {"mobile": phone, "sender_name": _unquote(service_name) or self.sender, "message": text,
                "response_type": "json"}
        try:
            r = self.client.post(self.url, json=body,
                                 headers={"h_api_key": self.api_key, "Content-Type": "application/json"})
        except httpx.TimeoutException:
            # It may have gone out. Never retry automatically: a member must not get the same SMS twice.
            return SendResult("unknown", description="Taifa unreachable: no reply in time; check delivery before resending")
        except httpx.HTTPError as e:
            return SendResult("failed", description=f"Taifa unreachable: {type(e).__name__}")
        row, error = taifa_reply(r.text)
        msg_id = str(row["message_id"])[:64] if row.get("message_id") is not None else None
        code = ("" if row.get("status_code") is None else str(row["status_code"]))[:10]  # fits the columns
        if error:
            return SendResult("failed", msg_id, code or None, error[:300])
        return SendResult("sent", msg_id, code, str(row.get("status_desc") or "Success")[:300])


def provider_from_env():
    kind = os.getenv("SAWAZI_SMS_PROVIDER", "simulate")
    if kind == "simulate":
        return SimulatedProvider()
    if kind == "taifa":
        key = _unquote(os.getenv("TAIFA_API_KEY")) or _unquote(os.getenv("SAWAZI_TAIFA_API_KEY"))
        if not key:
            raise RuntimeError("SAWAZI_SMS_PROVIDER=taifa needs TAIFA_API_KEY (or SAWAZI_TAIFA_API_KEY)")
        return TaifaProvider(key, os.getenv("SAWAZI_TAIFA_URL") or TAIFA_URL, sender=os.getenv("TAIFA_SENDER_ID"))
    raise RuntimeError(f"unknown SAWAZI_SMS_PROVIDER '{kind}' (use simulate or taifa)")


_provider = None


def get_provider():
    """FastAPI dependency; tests override it."""
    global _provider
    if _provider is None:
        _provider = provider_from_env()
    return _provider


def is_stop(text: str | None) -> bool:
    words = (text or "").strip().upper().replace(".", " ").split()
    return bool(words) and words[0] in STOP_WORDS
