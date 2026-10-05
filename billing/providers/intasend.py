"""IntaSend M-Pesa STK Push (https://developers.intasend.com).

Endpoints and headers follow IntaSend's official Python SDK (intasend-python):
POST payment/mpesa-stk-push/ and payment/status/ under /api/v1/, authenticated
with "Authorization: Bearer <secret key>" plus the INTASEND_PUBLIC_API_KEY header.
Webhooks carry the "challenge" string configured in the IntaSend dashboard.
"""

import json
import logging
import uuid
from decimal import Decimal, InvalidOperation
from hmac import compare_digest

import requests
from django.conf import settings

from . import CallbackRejected, InitResult, PaymentEvent, ProviderError

log = logging.getLogger(__name__)

BASE_URLS = {"sandbox": "https://sandbox.intasend.com/api/v1/", "live": "https://payment.intasend.com/api/v1/"}
STATES = {
    "COMPLETE": "paid",
    "FAILED": "failed",
    "CANCELED": "failed",
    "PARTIAL": "failed",  # paid less than asked: never PAID; the amount check flags it for a refund
    "PENDING": "pending",
    "PROCESSING": "pending",
    "RETRY": "pending",
}


def _decimal(value):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


class IntaSendProvider:
    name = "intasend"

    def __init__(self, env, publishable_key, secret_key, webhook_secret, session=None, timeout=30):
        if env not in BASE_URLS:
            raise ValueError(f"INTASEND_ENV must be one of {list(BASE_URLS)}")
        self.base_url = BASE_URLS[env]
        self.publishable_key = publishable_key
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_settings(cls):
        return cls(
            env=settings.INTASEND_ENV,
            publishable_key=settings.INTASEND_PUBLISHABLE_KEY,
            secret_key=settings.INTASEND_SECRET_KEY,
            webhook_secret=settings.INTASEND_WEBHOOK_SECRET,
        )

    @property
    def configured(self):
        return bool(self.secret_key and self.publishable_key)

    def _post(self, path, body):
        try:
            resp = self.session.post(
                self.base_url + path,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.secret_key}",
                    "INTASEND_PUBLIC_API_KEY": self.publishable_key,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ProviderError(f"IntaSend unreachable: {e}")
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            raise ProviderError(f"IntaSend error {resp.status_code}: {_error_text(data) or resp.text[:200]}")
        return data

    def initiate(self, order, phone):
        reference = f"subra-{order.pk}-{uuid.uuid4().hex[:8]}"
        data = self._post("payment/mpesa-stk-push/", {
            "public_key": self.publishable_key,
            "currency": "KES",
            "method": "M-PESA",
            "amount": int(order.price_kes),
            "phone_number": phone,
            "api_ref": reference,
            "narrative": "SUBRA deck polish",
        })
        invoice = data.get("invoice") or {}
        if not invoice.get("invoice_id"):
            raise ProviderError(f"IntaSend returned no invoice: {str(data)[:200]}")
        return InitResult(provider_ref=invoice["invoice_id"], reference=reference, raw=data)

    def handle_callback(self, body, headers):
        payload = json.loads(body)
        if not isinstance(payload, dict) or "invoice_id" not in payload:
            raise ValueError("Not an IntaSend webhook")
        challenge = str(payload.get("challenge") or "")
        if not self.webhook_secret or not compare_digest(challenge, self.webhook_secret):
            raise CallbackRejected("IntaSend webhook challenge mismatch")
        return self._event(payload, raw=payload)

    def check_status(self, ref):
        data = self._post("payment/status/", {"invoice_id": ref, "public_key": self.publishable_key})
        invoice = data.get("invoice")
        if not invoice:
            raise ProviderError(f"IntaSend status returned no invoice: {str(data)[:200]}")
        return self._event(invoice, raw=data)

    def _event(self, invoice, raw):
        state = STATES.get(str(invoice.get("state", "")).upper(), "pending")
        return PaymentEvent(
            provider_ref=str(invoice.get("invoice_id") or ""),
            state=state,
            amount=_decimal(invoice.get("value")),
            receipt=str(invoice.get("mpesa_reference") or ""),
            fee=_decimal(invoice.get("charges")),
            code=str(invoice.get("failed_code") or invoice.get("state") or ""),
            reason=str(invoice.get("failed_reason") or ""),
            reference=str(invoice.get("api_ref") or ""),
            raw=raw,
        )


def _error_text(data):
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list):
        return "; ".join(str(e.get("detail", e)) if isinstance(e, dict) else str(e) for e in errors)
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("message") or "")
    return ""
