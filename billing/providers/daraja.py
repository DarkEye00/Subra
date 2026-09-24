"""Safaricom Daraja (future provider): OAuth token, STK Push, and STK Push status query.

Not used at launch (IntaSend is, see Amendment 1A). Kept as a working provider for
when SUBRA has its own paybill or till; construct it with explicit credentials.
"""

import base64
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from . import InitResult, PaymentEvent, ProviderError

log = logging.getLogger(__name__)

BASE_URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
NAIROBI = ZoneInfo("Africa/Nairobi")
PENDING_ERROR_CODE = "500.001.1001"  # "The transaction is being processed"


class MpesaError(ProviderError):
    pass


class DarajaClient:
    def __init__(self, env, consumer_key, consumer_secret, shortcode, passkey, callback_url,
                 transaction_type="CustomerPayBillOnline", party_b="", session=None, token_cache=None,
                 timeout=30):
        if env not in BASE_URLS:
            raise ValueError(f"MPESA_ENV must be one of {list(BASE_URLS)}")
        self.base_url = BASE_URLS[env]
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.shortcode = shortcode
        self.passkey = passkey
        self.callback_url = callback_url
        self.transaction_type = transaction_type
        self.party_b = party_b or shortcode
        self.session = session or requests.Session()
        self.token_cache = token_cache if token_cache is not None else _TOKEN_CACHE
        self.timeout = timeout

    # --- auth -------------------------------------------------------------

    def _token(self):
        cached = self.token_cache.get(self.base_url)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        resp = self.session.get(
            f"{self.base_url}/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            auth=(self.consumer_key, self.consumer_secret),
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            raise MpesaError(f"Daraja auth failed ({resp.status_code}): {resp.text[:200]}")
        data = resp.json()
        ttl = int(data.get("expires_in", 3599)) - 60
        self.token_cache[self.base_url] = (data["access_token"], time.monotonic() + ttl)
        return data["access_token"]

    def _password(self, timestamp):
        return base64.b64encode(f"{self.shortcode}{self.passkey}{timestamp}".encode()).decode()

    @staticmethod
    def _timestamp():
        return datetime.now(NAIROBI).strftime("%Y%m%d%H%M%S")

    def _post(self, path, body):
        return self.session.post(
            f"{self.base_url}{path}",
            json=body,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=self.timeout,
        )

    # --- API --------------------------------------------------------------

    def stk_push(self, phone, amount, reference, description):
        timestamp = self._timestamp()
        body = {
            "BusinessShortCode": self.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "TransactionType": self.transaction_type,
            "Amount": int(amount),
            "PartyA": phone,
            "PartyB": self.party_b,
            "PhoneNumber": phone,
            "CallBackURL": self.callback_url,
            "AccountReference": reference[:12],
            "TransactionDesc": description[:13],
        }
        resp = self._post("/mpesa/stkpush/v1/processrequest", body)
        data = _json(resp)
        if resp.status_code != 200 or str(data.get("ResponseCode")) != "0":
            message = data.get("errorMessage") or data.get("ResponseDescription") or resp.text[:200]
            raise MpesaError(f"STK push failed: {message}")
        return data

    def query(self, checkout_request_id):
        """Status of an STK push. Returns {"pending": True} while M-Pesa is still processing."""
        timestamp = self._timestamp()
        body = {
            "BusinessShortCode": self.shortcode,
            "Password": self._password(timestamp),
            "Timestamp": timestamp,
            "CheckoutRequestID": checkout_request_id,
        }
        resp = self._post("/mpesa/stkpushquery/v1/query", body)
        data = _json(resp)
        if data.get("errorCode") == PENDING_ERROR_CODE:
            return {"pending": True}
        if resp.status_code != 200 or "ResultCode" not in data:
            raise MpesaError(f"STK query failed: {data.get('errorMessage') or resp.text[:200]}")
        data["ResultCode"] = str(data["ResultCode"])
        return data


_TOKEN_CACHE = {}


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return {}


class DarajaProvider:
    """PaymentProvider implementation on top of DarajaClient."""

    name = "daraja"

    def __init__(self, client):
        self.client = client

    def initiate(self, order, phone):
        reference = f"SUBRA{order.pk}"
        data = self.client.stk_push(phone, order.price_kes, reference=reference, description="Deck polish")
        return InitResult(provider_ref=data["CheckoutRequestID"], reference=reference, raw=data)

    def handle_callback(self, payload):
        # Daraja doesn't sign callbacks: protect the callback URL with a secret path segment.
        stk = payload["Body"]["stkCallback"]
        code = str(stk.get("ResultCode"))
        items = {i.get("Name"): i.get("Value") for i in stk.get("CallbackMetadata", {}).get("Item", [])}
        amount = items.get("Amount")
        return PaymentEvent(
            provider_ref=stk["CheckoutRequestID"],
            state="paid" if code == "0" else "failed",
            amount=None if amount is None else _dec(amount),
            receipt=str(items.get("MpesaReceiptNumber") or ""),
            code=code,
            reason=stk.get("ResultDesc", ""),
            raw=payload,
        )

    def check_status(self, ref):
        result = self.client.query(ref)
        if result.get("pending"):
            return PaymentEvent(provider_ref=ref, state="pending", raw=result)
        code = result["ResultCode"]
        return PaymentEvent(provider_ref=ref, state="paid" if code == "0" else "failed", code=code,
                            reason=result.get("ResultDesc", ""), raw=result)


def _dec(value):
    from decimal import Decimal

    return Decimal(str(value))
