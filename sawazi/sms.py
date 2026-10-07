"""Sending SMS to members, through Taifa Mobile.

Nothing is ever sent without a staff member approving it (see the /send endpoint).

Providers
- simulate (default): sends nothing, marks messages `simulated`. Taifa Mobile has no
  sandbox, so this is how to try the whole flow safely.
- taifa: real sends. Needs SAWAZI_SMS_PROVIDER=taifa, SAWAZI_TAIFA_API_KEY, and the
  institution's SMS settings switched on.

Taifa Mobile's protocol (from their reference library, github.com/taifaMobile/sms):
POST {base}/sms/ with JSON {"message": <hex>, "key": <api key>, "service_name": ...}
where <hex> is AES-128-CBC (PKCS7) of {"message": ..., "recepients": ...} [sic],
keyed with the API key (PHP openssl semantics: first 16 bytes, zero-padded if
shorter) and a fixed IV. Reply: {"messageId", "status", "statusDescription"}.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TAIFA_URL = "https://sms.taifamobile.co.ke/clientapi"
TAIFA_IV = b"w4^dgd$%^62:)dgs"  # fixed by Taifa's protocol, not ours to choose

TAIFA_STATUS = {
    "00": "Success",
    "01": "Failed",
    "97": "Taifa Mobile account has no enough funds",
    "98": "Service not found (check the institution's SMS service name)",
    "99": "Missing required details",
}

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


def php_json(obj) -> str:
    """json_encode() defaults, so the encrypted payload is byte-for-byte what Taifa's PHP client sends."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).replace("/", "\\/")


def taifa_encrypt(plaintext: str, api_key: str) -> str:
    key = api_key.encode()[:16].ljust(16, b"\0")
    padder = padding.PKCS7(128).padder()
    data = padder.update(plaintext.encode()) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(TAIFA_IV)).encryptor()
    return (enc.update(data) + enc.finalize()).hex()


class TaifaProvider:
    name = "taifa"

    def __init__(self, api_key: str, base_url: str = TAIFA_URL, transport: httpx.BaseTransport | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=20, transport=transport)

    def send(self, phone: str, text: str, service_name: str | None) -> SendResult:
        payload = taifa_encrypt(php_json({"message": text, "recepients": phone}), self.api_key)
        body = {"message": payload, "key": self.api_key, "service_name": service_name}
        try:
            r = self.client.post(f"{self.base_url}/sms/", json=body)
        except httpx.TimeoutException:
            # It may have gone out. Never retry automatically: a member must not get the same SMS twice.
            return SendResult("unknown", description="No reply from Taifa Mobile in time; check delivery before resending")
        except httpx.HTTPError as e:
            return SendResult("failed", description=f"Could not reach Taifa Mobile: {type(e).__name__}")
        try:
            data = r.json()
        except ValueError:
            return SendResult("unknown", description=f"Unreadable reply from Taifa Mobile (HTTP {r.status_code})")
        code = str(data.get("status", ""))
        desc = data.get("statusDescription") or TAIFA_STATUS.get(code, "Unknown status")
        if code == "00":
            return SendResult("sent", data.get("messageId"), code, desc)
        return SendResult("failed", data.get("messageId"), code, TAIFA_STATUS.get(code, desc))


def provider_from_env():
    kind = os.getenv("SAWAZI_SMS_PROVIDER", "simulate")
    if kind == "simulate":
        return SimulatedProvider()
    if kind == "taifa":
        key = os.getenv("SAWAZI_TAIFA_API_KEY")
        if not key:
            raise RuntimeError("SAWAZI_SMS_PROVIDER=taifa needs SAWAZI_TAIFA_API_KEY")
        return TaifaProvider(key, os.getenv("SAWAZI_TAIFA_URL", TAIFA_URL))
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
