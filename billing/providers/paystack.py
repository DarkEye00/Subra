"""Paystack M-Pesa charges (Amendment 2). https://paystack.com/docs/api/charge/

POST /charge with mobile_money {phone, provider: "mpesa"} sends the STK push. Paystack identifies a
transaction by the reference we send, so that reference is also our provider_ref. GET
/transaction/verify/<reference> reports the outcome. Webhooks are signed: X-Paystack-Signature is the
HMAC-SHA512 of the raw body, keyed with the secret key. Amounts and fees are in cents.

Confirmed in test mode on 5 Oct 2026 (fixtures in billing/tests/fixtures/paystack/): the phone must be
+2547XXXXXXXX (a 07... number is rejected as "Invalid phone number format"); the M-Pesa receipt is
data.receipt_number (a placeholder, "10101", in test mode); fees are in cents, like amount (1.5%).

Subscriptions (27 Oct spec): Paystack Plans run the monthly card billing. The plan, checkout and subscription
calls below follow Paystack's API reference; they have not been run against test mode yet (paystack.com was not
reachable from the build environment), so record real responses before launch (spec, section 11).
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
MONEY_MOVED = {"success", "reversed"}  # Paystack echoes the requested amount even when nothing was paid


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

    @property
    def mode(self):
        """"live" or "test", from the key. Paystack's two modes have separate plans, customers and cards."""
        if self.secret_key.startswith("sk_live_"):
            return "live"
        return "test" if self.secret_key.startswith("sk_test_") else ""

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

    def verify_signature(self, body, headers):
        signature = headers.get("X-Paystack-Signature") or ""
        expected = hmac.new(self.secret_key.encode(), body, hashlib.sha512).hexdigest()
        if not self.secret_key or not hmac.compare_digest(signature, expected):
            raise CallbackRejected("Paystack webhook signature mismatch")

    # --- plans and card checkout (subscriptions spec) --------------------------------------------------------

    def create_plan(self, name, price_kes):
        data = self._request("POST", "/plan", {
            "name": name, "amount": int(price_kes) * 100, "interval": "monthly", "currency": "KES",
            "send_invoices": True, "send_sms": False,
        })
        return data.get("data") or {}

    def update_plan(self, plan_code, name, price_kes):
        """A new amount applies to each subscriber's next charge."""
        self._request("PUT", f"/plan/{plan_code}", {
            "name": name, "amount": int(price_kes) * 100, "currency": "KES", "update_existing_subscriptions": True,
        })

    def fetch_plan(self, plan_code):
        return self._request("GET", f"/plan/{plan_code}").get("data") or {}

    def initialize_checkout(self, email, amount_kes, reference, callback_url, channels, plan_code="", metadata=None):
        """Paystack's hosted payment page. With a plan, the first successful charge creates the subscription
        (and Paystack charges the plan's own amount)."""
        body = {
            "email": email, "amount": int(amount_kes) * 100, "currency": "KES", "reference": reference,
            "callback_url": callback_url, "channels": channels, "metadata": metadata or {},
        }
        if plan_code:
            body["plan"] = plan_code
        data = self._request("POST", "/transaction/initialize", body)
        out = data.get("data") or {}
        if not out.get("authorization_url"):
            raise ProviderError("Paystack returned no checkout URL")
        return out

    def verify_transaction(self, reference):
        return self._request("GET", f"/transaction/verify/{reference}").get("data") or {}

    def list_transactions(self, customer_id, since):
        """Successful charges for one customer since a datetime (to find renewals whose webhook was lost)."""
        path = f"/transaction?customer={customer_id}&status=success&from={since.date().isoformat()}&perPage=50"
        return self._request("GET", path).get("data") or []

    def fetch_subscription(self, code):
        return self._request("GET", f"/subscription/{code}").get("data") or {}

    def list_subscriptions(self, customer_id):
        return self._request("GET", f"/subscription?customer={customer_id}&perPage=50").get("data") or []

    def manage_link(self, code):
        """A Paystack-hosted page where the customer updates the card (or cancels)."""
        link = (self._request("GET", f"/subscription/{code}/manage/link").get("data") or {}).get("link")
        if not link:
            raise ProviderError("Paystack returned no manage link")
        return link

    def disable_subscription(self, code, token):
        self._request("POST", "/subscription/disable", {"code": code, "token": token})

    def enable_subscription(self, code, token):
        self._request("POST", "/subscription/enable", {"code": code, "token": token})

    def event_for(self, data, raw):
        """A PaymentEvent from a transaction (a webhook's data, or a verify response's data)."""
        return self._event(data, raw=raw)

    # --- M-Pesa ---------------------------------------------------------------------------------------------

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
        self.verify_signature(body, headers)
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
            amount=_kes(data.get("amount")) if status in MONEY_MOVED else None,
            receipt=str(data.get("receipt_number") or ""),
            fee=_kes(data.get("fees")),
            code=status,
            reason=str(data.get("gateway_response") or ""),
            reference=str(data.get("reference") or ""),
            raw=raw,
        )
