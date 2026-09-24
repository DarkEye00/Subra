"""Safaricom Daraja client: OAuth token, STK Push, and STK Push status query."""

import base64
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from django.conf import settings

log = logging.getLogger(__name__)

BASE_URLS = {"sandbox": "https://sandbox.safaricom.co.ke", "production": "https://api.safaricom.co.ke"}
NAIROBI = ZoneInfo("Africa/Nairobi")
PENDING_ERROR_CODE = "500.001.1001"  # "The transaction is being processed"


class MpesaError(Exception):
    pass


def normalize_phone(raw):
    """Return a Safaricom mobile number as 2547XXXXXXXX or 2541XXXXXXXX, or raise ValueError."""
    raw = (raw or "").strip()
    if not re.fullmatch(r"\+?[\d\s]+", raw):
        raise ValueError("Enter a phone number like 0712 345 678")
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("254"):
        number = digits
    elif len(digits) == 10 and digits.startswith("0"):
        number = "254" + digits[1:]
    elif len(digits) == 9:
        number = "254" + digits
    else:
        raise ValueError("Enter a phone number like 0712 345 678")
    if number[3] not in "71":
        raise ValueError("Enter a Safaricom M-Pesa number starting 07 or 01")
    return number


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

    @classmethod
    def from_settings(cls):
        return cls(
            env=settings.MPESA_ENV,
            consumer_key=settings.MPESA_CONSUMER_KEY,
            consumer_secret=settings.MPESA_CONSUMER_SECRET,
            shortcode=settings.MPESA_SHORTCODE,
            passkey=settings.MPESA_PASSKEY,
            callback_url=settings.MPESA_CALLBACK_URL,
            transaction_type=settings.MPESA_TRANSACTION_TYPE,
            party_b=settings.MPESA_PARTY_B,
        )

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
