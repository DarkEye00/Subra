import base64
import json

import pytest

from billing.phone import normalize_phone
from billing.providers.daraja import DarajaClient, DarajaProvider, MpesaError


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("0712345678", "254712345678"),
        ("0112345678", "254112345678"),
        ("+254 712 345 678", "254712345678"),
        ("254712345678", "254712345678"),
        ("712345678", "254712345678"),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "0812345678", "25471234567", "0712-34-56-789", "not a phone"])
def test_normalize_phone_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_phone(raw)


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kw):
        self.requests.append(("GET", url, kw))
        return self.responses.pop(0)

    def post(self, url, **kw):
        self.requests.append(("POST", url, kw))
        return self.responses.pop(0)


def client(session):
    return DarajaClient(
        env="sandbox", consumer_key="key", consumer_secret="secret", shortcode="174379",
        passkey="pass", callback_url="https://example.com/cb", transaction_type="CustomerPayBillOnline",
        party_b="", session=session, token_cache={},
    )


TOKEN = FakeResponse(200, {"access_token": "tok", "expires_in": "3599"})


def test_stk_push_request_shape():
    session = FakeSession([TOKEN, FakeResponse(200, {
        "MerchantRequestID": "m1", "CheckoutRequestID": "ws_CO_1", "ResponseCode": "0",
        "ResponseDescription": "Success", "CustomerMessage": "Success"})])
    result = client(session).stk_push("254712345678", 350, reference="SUBRA42", description="Deck polish")
    assert result["CheckoutRequestID"] == "ws_CO_1"
    method, url, kw = session.requests[1]
    assert url == "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    assert kw["headers"]["Authorization"] == "Bearer tok"
    body = kw["json"]
    assert body["Amount"] == 350 and body["PartyA"] == "254712345678" and body["PhoneNumber"] == "254712345678"
    assert body["PartyB"] == "174379" and body["BusinessShortCode"] == "174379"
    assert body["CallBackURL"] == "https://example.com/cb"
    assert base64.b64decode(body["Password"]).decode() == "174379" + "pass" + body["Timestamp"]
    assert len(body["AccountReference"]) <= 12 and len(body["TransactionDesc"]) <= 13


def test_token_is_cached_between_calls():
    ok = {"MerchantRequestID": "m", "CheckoutRequestID": "c", "ResponseCode": "0"}
    session = FakeSession([TOKEN, FakeResponse(200, ok), FakeResponse(200, ok)])
    c = client(session)
    c.stk_push("254712345678", 1, "R", "D")
    c.stk_push("254712345678", 1, "R", "D")
    assert [r[0] for r in session.requests] == ["GET", "POST", "POST"]


def test_stk_push_error_raises():
    session = FakeSession([TOKEN, FakeResponse(400, {"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid PhoneNumber"})])
    with pytest.raises(MpesaError) as exc:
        client(session).stk_push("254712345678", 1, "R", "D")
    assert "Invalid PhoneNumber" in str(exc.value)


def test_query_reports_pending_while_processing():
    session = FakeSession([TOKEN, FakeResponse(500, {"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})])
    assert client(session).query("ws_CO_1") == {"pending": True}


def test_query_returns_result_code():
    session = FakeSession([TOKEN, FakeResponse(200, {"ResponseCode": "0", "ResultCode": "1032", "ResultDesc": "Request cancelled by user"})])
    result = client(session).query("ws_CO_1")
    assert result["ResultCode"] == "1032" and not result.get("pending")


def test_daraja_provider_normalises_callbacks():
    payload = {"Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_1", "ResultCode": 0, "ResultDesc": "ok",
                                        "CallbackMetadata": {"Item": [{"Name": "Amount", "Value": 350},
                                                                      {"Name": "MpesaReceiptNumber", "Value": "R1"}]}}}}
    event = DarajaProvider(client=None).handle_callback(payload)
    assert (event.provider_ref, event.state, event.receipt, int(event.amount)) == ("ws_CO_1", "paid", "R1", 350)
