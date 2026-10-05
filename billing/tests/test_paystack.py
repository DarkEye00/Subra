import hashlib
import hmac
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from billing.models import Payment
from billing.providers import CallbackRejected, ProviderError, get_provider
from billing.providers.paystack import PaystackProvider, paystack_phone

FIXTURES = Path(__file__).parent / "fixtures" / "paystack"
SECRET = "sk_test_subra"  # the key in subra.test_settings


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = json.dumps(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def provider(session=None, secret=SECRET):
    return PaystackProvider(secret_key=secret, session=session or FakeSession([]))


ORDER = SimpleNamespace(pk=42, price_kes=200, public_id="5b7f0c1e-0000-4000-8000-000000000042",
                        user=SimpleNamespace(email="wanjiku@example.co.ke"))


def signed(payload, secret=SECRET):
    body = json.dumps(payload).encode()
    return body, {"X-Paystack-Signature": hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()}


# --- starting a charge ------------------------------------------------------

def test_initiate_sends_an_mpesa_charge():
    session = FakeSession([FakeResponse(200, load("charge_response.json")["body"])])
    result = provider(session).initiate(ORDER, "254712345678")
    method, url, kw = session.requests[0]
    assert (method, url) == ("POST", "https://api.paystack.co/charge")
    assert kw["headers"]["Authorization"] == f"Bearer {SECRET}"
    body = kw["json"]
    assert body["amount"] == 20000 and body["currency"] == "KES" and body["email"] == "wanjiku@example.co.ke"
    assert body["mobile_money"] == {"phone": paystack_phone("254712345678"), "provider": "mpesa"}
    assert body["reference"].startswith("subra-42-")
    assert result.provider_ref == result.reference == body["reference"]


def test_phone_numbers_are_sent_in_the_format_paystack_accepted():
    assert paystack_phone("254712345678") == "+254712345678"


def test_initiate_error_raises_provider_error():
    session = FakeSession([FakeResponse(400, {"status": False, "message": "Invalid phone number"})])
    with pytest.raises(ProviderError) as exc:
        provider(session).initiate(ORDER, "254712345678")
    assert "Invalid phone number" in str(exc.value)


def test_an_immediately_failed_charge_raises_provider_error():
    session = FakeSession([FakeResponse(200, {"status": True, "data": {"status": "failed",
                                                                        "gateway_response": "Declined"}})])
    with pytest.raises(ProviderError) as exc:
        provider(session).initiate(ORDER, "254712345678")
    assert "Declined" in str(exc.value)


def test_network_errors_raise_provider_error():
    with pytest.raises(ProviderError):
        provider(FakeSession([requests.ConnectionError("no route")])).initiate(ORDER, "254712345678")


# --- status checks ----------------------------------------------------------

def test_check_status_reads_a_successful_charge():
    verify = load("verify_success.json")
    data = verify["data"]
    session = FakeSession([FakeResponse(200, verify)])
    event = provider(session).check_status(data["reference"])
    method, url, _ = session.requests[0]
    assert (method, url) == ("GET", f"https://api.paystack.co/transaction/verify/{data['reference']}")
    assert (event.state, event.provider_ref) == ("paid", data["reference"])
    assert event.amount == Decimal(data["amount"]) / 100
    assert event.fee == (Decimal(data["fees"]) / 100 if data.get("fees") is not None else None)
    assert event.receipt == str(data.get("receipt_number") or "")


@pytest.mark.parametrize("status, state", [("success", "paid"), ("failed", "failed"), ("abandoned", "failed"),
                                           ("reversed", "failed"), ("ongoing", "pending"), ("pending", "pending")])
def test_check_status_maps_paystack_statuses(status, state):
    session = FakeSession([FakeResponse(200, {"status": True, "data": {"status": status,
                                                                        "reference": "subra-42-x", "amount": 20000}})])
    assert provider(session).check_status("subra-42-x").state == state


def test_check_status_server_error_is_provider_error():
    with pytest.raises(ProviderError):
        provider(FakeSession([FakeResponse(500, {})])).check_status("subra-42-x")


# --- webhooks ---------------------------------------------------------------

def test_signed_charge_success_becomes_a_paid_event():
    payload = load("webhook_charge_success.json")
    body, headers = signed(payload)
    event = provider().handle_callback(body, headers)
    assert (event.state, event.provider_ref) == ("paid", payload["data"]["reference"])
    assert event.amount == Decimal(payload["data"]["amount"]) / 100


@pytest.mark.parametrize("headers", [{}, {"X-Paystack-Signature": "0" * 128}])
def test_unsigned_or_wrongly_signed_webhooks_are_rejected(headers):
    body = json.dumps(load("webhook_charge_success.json")).encode()
    with pytest.raises(CallbackRejected):
        provider().handle_callback(body, headers)


def test_webhooks_are_rejected_without_a_secret_key():
    body, headers = signed(load("webhook_charge_success.json"), secret="")
    with pytest.raises(CallbackRejected):
        provider(secret="").handle_callback(body, headers)


def test_signed_but_malformed_webhooks_are_value_errors():
    body = b"{not json"
    headers = {"X-Paystack-Signature": hmac.new(SECRET.encode(), body, hashlib.sha512).hexdigest()}
    with pytest.raises(ValueError):
        provider().handle_callback(body, headers)


def test_other_events_are_ignored():
    body, headers = signed({"event": "transfer.success", "data": {"reference": "TRF-1"}})
    assert provider().handle_callback(body, headers).state == "pending"


def test_paystack_is_the_default_provider_and_knows_whether_it_has_keys(settings):
    assert get_provider().name == "paystack" and get_provider().configured
    settings.PAYSTACK_SECRET_KEY = ""
    assert not get_provider().configured


# --- the webhook endpoint, end to end ----------------------------------------

def post_webhook(client, body, signature):
    return client.post("/billing/webhooks/paystack/", data=body, content_type="application/json",
                       HTTP_X_PAYSTACK_SIGNATURE=signature)


def test_webhook_endpoint_marks_a_paystack_payment_paid(client, quoted_order):
    payload = load("webhook_charge_success.json")
    data = payload["data"]
    payment = Payment.objects.create(order=quoted_order, provider="paystack", provider_ref=data["reference"],
                                     reference=data["reference"], phone="254712345678",
                                     amount_kes=int(data["amount"]) // 100)
    body, headers = signed(payload)
    assert post_webhook(client, body, headers["X-Paystack-Signature"]).status_code == 200
    payment.refresh_from_db()
    assert payment.status == "paid" and payment.raw_callbacks[0]["event"] == "charge.success"


def test_webhook_endpoint_rejects_a_bad_signature(client, db):
    assert post_webhook(client, b'{"event": "charge.success", "data": {}}', "nope").status_code == 403


def test_webhook_for_a_charge_we_never_made_is_accepted_and_ignored(client, db):
    body, headers = signed({"event": "charge.success",
                            "data": {"reference": "someone-else-1", "status": "success", "amount": 5000}})
    assert post_webhook(client, body, headers["X-Paystack-Signature"]).status_code == 200
    assert not Payment.objects.exists()


@pytest.mark.parametrize("status, has_amount", [("success", True), ("reversed", True), ("failed", False),
                                                ("abandoned", False), ("ongoing", False)])
def test_only_charges_where_money_moved_report_an_amount(status, has_amount):
    """Paystack echoes the requested amount on every transaction; a failed or abandoned prompt moved no money."""
    session = FakeSession([FakeResponse(200, {"status": True, "data": {"status": status, "reference": "subra-42-x",
                                                                        "amount": 20000}})])
    assert (provider(session).check_status("subra-42-x").amount is not None) is has_amount


def test_a_cancelled_prompt_sends_no_money_alert(order_factory):
    from django.core import mail

    from billing import services
    from orders.models import Order

    order = order_factory(status=Order.Status.READY)
    payment = Payment.objects.create(order=order, provider="paystack", provider_ref="subra-1-x", reference="subra-1-x",
                                     phone="254712345678", amount_kes=350)
    session = FakeSession([FakeResponse(200, {"status": True, "data": {
        "status": "abandoned", "reference": "subra-1-x", "amount": 35000, "gateway_response": "Customer cancelled"}})])
    services.check_payment(payment, provider=provider(session))
    payment.refresh_from_db()
    assert payment.status == "failed" and mail.outbox == []
