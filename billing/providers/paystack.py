"""Paystack M-Pesa charges (Amendment 2). https://paystack.com/docs/api/charge/

POST /charge with mobile_money {phone, provider: "mpesa"} sends the STK push. Paystack identifies a
transaction by the reference we send, so that reference is also our provider_ref. GET
/transaction/verify/<reference> reports the outcome. Webhooks are signed: X-Paystack-Signature is the
HMAC-SHA512 of the raw body, keyed with the secret key. Amounts and fees are in cents.

Confirmed in test mode on 5 Oct 2026 (fixtures in billing/tests/fixtures/paystack/): the phone must be
+2547XXXXXXXX (a 07... number is rejected as "Invalid phone number format"); the M-Pesa receipt is
data.receipt_number (a placeholder, "10101", in test mode); fees are in cents, like amount (1.5%).
"""

import hashlib
import hmac
import json
import uuid
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings

from . import CallbackRejected, InitResult, PaymentEvent, ProviderError

API = "https://api.paystack.co"
PAID = {"success"}
FAILED = {"failed", "abandoned", "reversed"}


def paystack_phone(phone):
    """Our 2547XXXXXXXX form, in the format Paystack's charge API accepted in test mode."""
    return "+" + phone


def _kes(cents):
    if cents in (None, ""):
        return None
    try:
        return (Decimal(str(cents)) / 100).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


class PaystackProvider:
    name = "paystack"

    def __init__(self, secret_key, session=None, timeout=30):
        self.secret_key = secret_key
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_settings(cls):
        return cls(secret_key=settings.PAYSTACK_SECRET_KEY)

    @property
    def configured(self):
        return bool(self.secret_key)

    def _request(self, method, path, body=None):
        try:
            resp = self.session.request(method, API + path, json=body, timeout=self.timeout,
                                        headers={"Authorization": f"Bearer {self.secret_key}"})
        except requests.RequestException as e:
            raise ProviderError(f"Paystack unreachable: {e}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400 or data.get("status") is False:
            raise ProviderError(f"Paystack error {resp.status_code}: {data.get('message') or resp.text[:200]}")
        return data

    def initiate(self, order, phone):
        reference = f"subra-{order.pk}-{uuid.uuid4().hex[:8]}"
        data = self._request("POST", "/charge", {
            "email": order.user.email,
            "amount": int(order.price_kes) * 100,
            "currency": "KES",
            "reference": reference,
            "mobile_money": {"phone": paystack_phone(phone), "provider": "mpesa"},
            "metadata": {"order": str(order.public_id)},
        })
        charge = data.get("data") or {}
        if charge.get("status") in FAILED:
            raise ProviderError(f"Paystack refused the charge: {charge.get('gateway_response') or data.get('message')}")
        return InitResult(provider_ref=reference, reference=reference, raw=data)

    def check_status(self, ref):
        data = self._request("GET", f"/transaction/verify/{ref}")
        return self._event(data.get("data") or {}, raw=data, fallback_ref=ref)

    def handle_callback(self, body, headers):
        signature = headers.get("X-Paystack-Signature") or ""
        expected = hmac.new(self.secret_key.encode(), body, hashlib.sha512).hexdigest()
        if not self.secret_key or not hmac.compare_digest(signature, expected):
            raise CallbackRejected("Paystack webhook signature mismatch")
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ValueError("Not a Paystack event")
        data = payload["data"]
        if payload.get("event") != "charge.success":
            return PaymentEvent(provider_ref=str(data.get("reference") or ""), state="pending", raw=payload)
        return self._event(data, raw=payload)

    def _event(self, data, raw, fallback_ref=""):
        status = str(data.get("status") or "")
        state = "paid" if status in PAID else "failed" if status in FAILED else "pending"
        return PaymentEvent(
            provider_ref=str(data.get("reference") or fallback_ref),
            state=state,
            amount=_kes(data.get("amount")),
            receipt=str(data.get("receipt_number") or ""),
            fee=_kes(data.get("fees")),
            code=status,
            reason=str(data.get("gateway_response") or ""),
            reference=str(data.get("reference") or ""),
            raw=raw,
        )
