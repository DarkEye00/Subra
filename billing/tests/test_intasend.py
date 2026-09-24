from decimal import Decimal
from types import SimpleNamespace

import pytest

from billing.providers import CallbackRejected, ProviderError
from billing.providers.intasend import IntaSendProvider


class FakeResponse:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data
        self.text = str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, **kw):
        self.requests.append((url, kw))
        return self.responses.pop(0)


def provider(session, env="sandbox"):
    return IntaSendProvider(env=env, publishable_key="ISPubKey_test", secret_key="ISSecretKey_test",
                            webhook_secret="challenge-123", session=session)


ORDER = SimpleNamespace(pk=42, price_kes=350, public_id=SimpleNamespace(hex="abcdef0123456789"))


def invoice(state="PENDING", **extra):
    base = {"invoice_id": "XMSLWOS", "state": state, "provider": "M-PESA", "charges": "0.00",
            "net_amount": "350.00", "value": "350.00", "api_ref": "subra-42-x", "failed_reason": None,
            "failed_code": None, "mpesa_reference": None}
    base.update(extra)
    return base


def test_initiate_sends_stk_push_with_both_keys():
    session = FakeSession([FakeResponse(200, {"invoice": invoice()})])
    result = provider(session).initiate(ORDER, "254712345678")
    url, kw = session.requests[0]
    assert url == "https://sandbox.intasend.com/api/v1/payment/mpesa-stk-push/"
    assert kw["headers"]["Authorization"] == "Bearer ISSecretKey_test"
    assert kw["headers"]["INTASEND_PUBLIC_API_KEY"] == "ISPubKey_test"
    body = kw["json"]
    assert body["method"] == "M-PESA" and body["currency"] == "KES"
    assert body["amount"] == 350 and body["phone_number"] == "254712345678"
    assert body["public_key"] == "ISPubKey_test"
    assert result.provider_ref == "XMSLWOS"
    assert result.reference == body["api_ref"] and result.reference.startswith("subra-42-")


def test_live_environment_uses_the_live_host():
    session = FakeSession([FakeResponse(200, {"invoice": invoice()})])
    provider(session, env="live").initiate(ORDER, "254712345678")
    assert session.requests[0][0].startswith("https://payment.intasend.com/api/v1/")


def test_initiate_error_raises_provider_error():
    session = FakeSession([FakeResponse(400, {"errors": [{"detail": "Invalid phone number"}]})])
    with pytest.raises(ProviderError) as exc:
        provider(session).initiate(ORDER, "254712345678")
    assert "Invalid phone number" in str(exc.value)


def test_callback_with_wrong_challenge_is_rejected():
    with pytest.raises(CallbackRejected):
        provider(FakeSession([])).handle_callback(invoice("COMPLETE", challenge="nope"))


def test_callback_without_challenge_is_rejected():
    with pytest.raises(CallbackRejected):
        provider(FakeSession([])).handle_callback(invoice("COMPLETE"))


def test_complete_callback_becomes_a_paid_event():
    event = provider(FakeSession([])).handle_callback(
        invoice("COMPLETE", challenge="challenge-123", charges="15.75", net_amount="334.25",
                mpesa_reference="SJK1ABC2DE"))
    assert (event.provider_ref, event.state, event.receipt) == ("XMSLWOS", "paid", "SJK1ABC2DE")
    assert event.amount == Decimal("350.00") and event.fee == Decimal("15.75")
    assert event.reference == "subra-42-x"


@pytest.mark.parametrize("state", ["FAILED", "CANCELED"])
def test_failed_callback_becomes_a_failed_event(state):
    event = provider(FakeSession([])).handle_callback(
        invoice(state, challenge="challenge-123", failed_reason="Request cancelled by user", failed_code="1032"))
    assert (event.state, event.code, event.reason) == ("failed", "1032", "Request cancelled by user")


@pytest.mark.parametrize("state", ["PENDING", "PROCESSING", "RETRY"])
def test_in_progress_states_are_pending(state):
    assert provider(FakeSession([])).handle_callback(invoice(state, challenge="challenge-123")).state == "pending"


def test_partial_payment_is_not_paid():
    event = provider(FakeSession([])).handle_callback(invoice("PARTIAL", challenge="challenge-123", value="100.00"))
    assert event.state == "failed" and event.amount == Decimal("100.00")


def test_check_status_reads_the_invoice():
    session = FakeSession([FakeResponse(200, {"invoice": invoice("COMPLETE", mpesa_reference="R1", charges="15.75")})])
    event = provider(session).check_status("XMSLWOS")
    url, kw = session.requests[0]
    assert url == "https://sandbox.intasend.com/api/v1/payment/status/"
    assert kw["json"] == {"invoice_id": "XMSLWOS", "public_key": "ISPubKey_test"}
    assert (event.state, event.receipt, event.fee) == ("paid", "R1", Decimal("15.75"))


def test_check_status_server_error_is_provider_error():
    with pytest.raises(ProviderError):
        provider(FakeSession([FakeResponse(500, {})])).check_status("XMSLWOS")
