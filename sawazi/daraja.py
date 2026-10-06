"""M-Pesa Daraja C2B: payments to the paybill arrive in real time instead of waiting for the statement.

Sawazi only listens. It never rejects a payment (validation always accepts) and never moves
money; register URLs with ResponseType "Completed" so a member's payment goes through even if
Sawazi is down. The paybill statement stays the source of truth: every callback is confirmed
against it on the next statement upload, and differences become high-severity exceptions.

Callback body (validation and confirmation are the same shape):
{"TransactionType": "Pay Bill", "TransID": "RKTQDM7W6S", "TransTime": "20191122063845",
 "TransAmount": "10.00", "BusinessShortCode": "600638", "BillRefNumber": "UT00104",
 "InvoiceNumber": "", "OrgAccountBalance": "", "ThirdPartyTransID": "",
 "MSISDN": "254708374149" (often masked or hashed by Safaricom), "FirstName": "JOHN", ...}
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from .importers.common import norm_phone, to_cents

BASE_URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}

ACCEPT = {"ResultCode": 0, "ResultDesc": "Accepted"}

# Daraja refuses callback URLs containing these words (case-insensitive).
FORBIDDEN_URL_WORDS = ("mpesa", "m-pesa", "safaricom", "exec", "cmd", "sql", "query")

_PLAIN_MSISDN = re.compile(r"^\+?\d{9,12}$")


@dataclass
class C2BPayment:
    trans_id: str
    shortcode: str
    amount_cents: int
    txn_time: datetime
    account_ref: str | None
    payer_phone: str | None
    payer_name: str | None
    transaction_type: str | None


class BadCallback(ValueError):
    pass


def parse(body: dict) -> C2BPayment:
    trans_id = str(body.get("TransID") or "").strip()
    shortcode = str(body.get("BusinessShortCode") or "").strip()
    if not trans_id or not shortcode:
        raise BadCallback("missing TransID or BusinessShortCode")
    amount = to_cents(str(body.get("TransAmount") or ""))
    if amount <= 0:
        raise BadCallback(f"bad amount {body.get('TransAmount')!r}")
    try:
        when = datetime.strptime(str(body.get("TransTime")), "%Y%m%d%H%M%S")  # Nairobi time, like the statement
    except ValueError:
        raise BadCallback(f"bad TransTime {body.get('TransTime')!r}") from None
    raw_msisdn = re.sub(r"\s", "", str(body.get("MSISDN") or ""))
    # Safaricom now often masks ("2547****149") or hashes the number: only trust a plain number.
    phone = norm_phone(raw_msisdn) if _PLAIN_MSISDN.match(raw_msisdn) else None
    name = " ".join(str(body.get(k) or "").strip() for k in ("FirstName", "MiddleName", "LastName")).split()
    return C2BPayment(
        trans_id=trans_id,
        shortcode=shortcode,
        amount_cents=amount,
        txn_time=when,
        account_ref=str(body.get("BillRefNumber") or "").strip() or None,
        payer_phone=phone,
        payer_name=" ".join(name).upper() or None,
        transaction_type=str(body.get("TransactionType") or "").strip() or None,
    )


# ---------------------------------------------------------------- Daraja API (URL registration, sandbox tests)

class DarajaClient:
    """Used once per paybill to register Sawazi's URLs, and in the sandbox to simulate payments.
    Credentials come from the caller (environment), never from the database."""

    def __init__(self, consumer_key: str, consumer_secret: str, env: str = "sandbox",
                 transport: httpx.BaseTransport | None = None):
        if env not in BASE_URLS:
            raise ValueError("env must be sandbox or production")
        self.base = BASE_URLS[env]
        self.env = env
        self.http = httpx.Client(timeout=30, transport=transport)
        self._auth = base64.b64encode(f"{consumer_key}:{consumer_secret}".encode()).decode()

    def token(self) -> str:
        r = self.http.get(f"{self.base}/oauth/v1/generate", params={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {self._auth}"})
        r.raise_for_status()
        return r.json()["access_token"]

    def _post(self, path: str, body: dict) -> dict:
        r = self.http.post(f"{self.base}{path}", json=body, headers={"Authorization": f"Bearer {self.token()}"})
        data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"Daraja {path} failed (HTTP {r.status_code}): {data}")
        return data

    def register_urls(self, shortcode: str, confirmation_url: str, validation_url: str) -> dict:
        for url in (confirmation_url, validation_url):
            check_callback_url(url)
        return self._post("/mpesa/c2b/v2/registerurl", {
            "ShortCode": shortcode,
            "ResponseType": "Completed",  # if Sawazi is unreachable, the member's payment still completes
            "ConfirmationURL": confirmation_url,
            "ValidationURL": validation_url,
        })

    def simulate(self, shortcode: str, amount: int, msisdn: str, bill_ref: str) -> dict:
        if self.env != "sandbox":
            raise RuntimeError("simulate only exists in the sandbox")
        return self._post("/mpesa/c2b/v1/simulate", {
            "ShortCode": shortcode, "CommandID": "CustomerPayBillOnline", "Amount": amount,
            "Msisdn": msisdn, "BillRefNumber": bill_ref,
        })


def check_callback_url(url: str) -> None:
    if not url.startswith("https://"):
        raise ValueError(f"Daraja needs https callback URLs: {url}")
    bad = [w for w in FORBIDDEN_URL_WORDS if w in url.lower()]
    if bad:
        raise ValueError(f"Daraja rejects URLs containing {bad}: {url}")
