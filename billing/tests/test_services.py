import json
from decimal import Decimal

import pytest
from django.core import mail

from billing import services
from billing.models import Payment
from billing.providers import InitResult, PaymentEvent, ProviderError
from orders.models import Order

S = Order.Status


class FakeProvider:
    name = "intasend"

    def __init__(self, ref="INV-1", status=None, fail=False):
        self.ref, self.status, self.fail = ref, status, fail
        self.initiated = []

    def initiate(self, order, phone):
        if self.fail:
            raise ProviderError("down")
        self.initiated.append((order.pk, phone, order.price_kes))
        return InitResult(provider_ref=self.ref, reference=f"subra-{order.pk}-t", raw={"invoice": {"invoice_id": self.ref}})

    def handle_callback(self, body, headers):
        return event(**json.loads(body))

    def check_status(self, ref):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status


def event(ref="INV-1", state="paid", amount="350", receipt="SJK1ABC2DE", fee="15.75", code="", reason="",
          reference=""):
    return PaymentEvent(provider_ref=ref, state=state, amount=None if amount is None else Decimal(amount),
                        receipt=receipt, fee=None if fee is None else Decimal(fee), code=code, reason=reason,
                        reference=reference, raw={"state": state, "ref": ref})


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "enqueue_processing", lambda order: calls.append(order.pk))
    monkeypatch.setattr(services, "schedule_status_check", lambda payment: None)
    return calls


def pending_payment(order, ref="INV-1"):
    return services.start_payment(order, "0712 345 678", provider=FakeProvider(ref=ref))


def callback(**kw):
    return services.handle_callback("intasend", json.dumps(kw).encode(), {}, provider=FakeProvider())


# --- starting a payment ---------------------------------------------------

def test_start_payment_sends_stk_push_and_waits(quoted_order, enqueued):
    provider = FakeProvider()
    payment = services.start_payment(quoted_order, "0712 345 678", provider=provider)
    assert provider.initiated == [(quoted_order.pk, "254712345678", 350)]
    assert (payment.provider, payment.provider_ref, payment.reference) == ("intasend", "INV-1", f"subra-{quoted_order.pk}-t")
    assert (payment.status, payment.amount_kes) == ("pending", 350)
    assert payment.raw_init == {"invoice": {"invoice_id": "INV-1"}}
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_PENDING
    assert quoted_order.user.mpesa_phone == "254712345678"


def test_start_payment_rejects_bad_phone(quoted_order, enqueued):
    with pytest.raises(services.PaymentError):
        services.start_payment(quoted_order, "12345", provider=FakeProvider())


def test_start_payment_needs_a_payable_order(quoted_order, enqueued):
    pending_payment(quoted_order)
    with pytest.raises(services.PaymentError):
        services.start_payment(quoted_order, "0712345678", provider=FakeProvider(ref="INV-2"))


def test_failed_push_leaves_order_quoted(quoted_order, enqueued):
    with pytest.raises(services.PaymentError):
        services.start_payment(quoted_order, "0712345678", provider=FakeProvider(fail=True))
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.QUOTED
    assert not Payment.objects.exists()


# --- callbacks --------------------------------------------------------------

def test_paid_callback_marks_paid_logs_fee_and_starts_processing(quoted_order, enqueued, django_capture_on_commit_callbacks):
    pending_payment(quoted_order)
    with django_capture_on_commit_callbacks(execute=True):
        payment = callback(ref="INV-1")
    assert (payment.status, payment.provider_receipt) == ("paid", "SJK1ABC2DE")
    assert payment.fee_kes == Decimal("15.75") and not payment.fee_estimated
    assert payment.raw_callbacks == [{"state": "paid", "ref": "INV-1"}]
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAID
    assert enqueued == [quoted_order.pk]


def test_missing_fee_is_estimated(quoted_order, enqueued, settings):
    settings.PAYMENT_FEE_RATE = 0.045
    pending_payment(quoted_order)
    payment = callback(ref="INV-1", fee="0.00")
    assert payment.fee_kes == Decimal("15.75") and payment.fee_estimated


def test_duplicate_callback_changes_nothing(quoted_order, enqueued, django_capture_on_commit_callbacks):
    pending_payment(quoted_order)
    with django_capture_on_commit_callbacks(execute=True):
        callback(ref="INV-1")
        payment = callback(ref="INV-1", receipt="OTHER")
    assert payment.provider_receipt == "SJK1ABC2DE"
    assert payment.callback_count == 2 and len(payment.raw_callbacks) == 2
    assert enqueued == [quoted_order.pk]
    assert quoted_order.events.filter(to_status=S.PAID).count() == 1


def test_pending_callback_changes_nothing(quoted_order, enqueued):
    pending_payment(quoted_order)
    payment = callback(ref="INV-1", state="pending")
    assert payment.status == "pending"
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_PENDING


def test_failed_callback_marks_order_payment_failed(quoted_order, enqueued):
    pending_payment(quoted_order)
    payment = callback(ref="INV-1", state="failed", code="1032", reason="Request cancelled by user")
    assert payment.status == "failed" and payment.result_code == "1032"
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_FAILED
    assert enqueued == []


def test_amount_mismatch_is_not_paid_and_alerts_admin(quoted_order, enqueued):
    pending_payment(quoted_order)
    payment = callback(ref="INV-1", amount="1")
    assert payment.status == "failed"
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_FAILED
    assert "amount" in mail.outbox[0].body.lower()


def test_paid_event_without_an_amount_is_not_trusted(quoted_order, enqueued):
    pending_payment(quoted_order)
    payment = callback(ref="INV-1", amount=None)
    assert payment.status == "pending"


def test_reference_mismatch_is_ignored(quoted_order, enqueued):
    pending_payment(quoted_order)
    payment = callback(ref="INV-1", reference="subra-999-x")
    assert payment.status == "pending"


def test_unknown_ref_is_ignored(quoted_order, enqueued):
    assert callback(ref="nope") is None


def test_late_success_after_failure_still_pays(quoted_order, enqueued, django_capture_on_commit_callbacks):
    payment = pending_payment(quoted_order)
    services.check_payment(payment, provider=FakeProvider(status=event(state="failed", code="1037", reason="timeout")))
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_FAILED
    with django_capture_on_commit_callbacks(execute=True):
        payment = callback(ref="INV-1")
    quoted_order.refresh_from_db()
    assert payment.status == "paid" and quoted_order.status == S.PAID
    assert enqueued == [quoted_order.pk]


def test_second_successful_payment_is_flagged_for_refund(quoted_order, enqueued, django_capture_on_commit_callbacks):
    first = pending_payment(quoted_order, ref="INV-1")
    services.check_payment(first, provider=FakeProvider(status=event(state="failed", code="1037")))
    services.start_payment(quoted_order, "0712345678", provider=FakeProvider(ref="INV-2"))
    with django_capture_on_commit_callbacks(execute=True):
        callback(ref="INV-2", receipt="R2")
        dup = callback(ref="INV-1", receipt="R1")
    assert dup.status == "paid" and "refund" in dup.result_desc.lower()
    assert enqueued == [quoted_order.pk]
    assert any("refund" in m.subject.lower() for m in mail.outbox)


# --- status checks ----------------------------------------------------------

def test_status_check_pending_changes_nothing(quoted_order, enqueued):
    payment = pending_payment(quoted_order)
    assert services.check_payment(payment, provider=FakeProvider(status=event(state="pending"))) == "pending"
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.status_checks == 1 and payment.callback_count == 0


def test_status_check_success_marks_paid(quoted_order, enqueued, django_capture_on_commit_callbacks):
    payment = pending_payment(quoted_order)
    with django_capture_on_commit_callbacks(execute=True):
        assert services.check_payment(payment, provider=FakeProvider(status=event())) == "paid"
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAID


def test_status_check_outage_keeps_waiting(quoted_order, enqueued):
    payment = pending_payment(quoted_order)
    assert services.check_payment(payment, provider=FakeProvider(status=ProviderError("down"))) == "pending"


# --- IntaSend webhook endpoint ----------------------------------------------

def intasend_payload(payment, **extra):
    data = {"invoice_id": payment.provider_ref, "state": "COMPLETE", "provider": "M-PESA", "charges": "15.75",
            "net_amount": "334.25", "currency": "KES", "value": "350.00", "api_ref": payment.reference,
            "mpesa_reference": "SJK1ABC2DE", "failed_reason": None, "failed_code": None,
            "challenge": "challenge-123"}
    data.update(extra)
    return data


def test_webhook_rejects_a_wrong_challenge(client, quoted_order, enqueued):
    payment = pending_payment(quoted_order)
    resp = client.post("/billing/webhooks/intasend/", data=json.dumps(intasend_payload(payment, challenge="x")),
                       content_type="application/json")
    assert resp.status_code == 403
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.raw_callbacks == []


def test_webhook_marks_paid(client, quoted_order, enqueued):
    payment = pending_payment(quoted_order)
    resp = client.post("/billing/webhooks/intasend/", data=json.dumps(intasend_payload(payment)),
                       content_type="application/json")
    assert resp.status_code == 200
    payment.refresh_from_db()
    assert (payment.status, payment.provider_receipt, payment.fee_kes) == ("paid", "SJK1ABC2DE", Decimal("15.75"))
    assert payment.raw_callbacks[0]["invoice_id"] == "INV-1"


def test_webhook_rejects_malformed_json(client, db):
    resp = client.post("/billing/webhooks/intasend/", data="{not json", content_type="application/json")
    assert resp.status_code == 400


def test_webhook_for_an_unknown_provider_is_404(client, db):
    assert client.post("/billing/webhooks/stripe/", data="{}", content_type="application/json").status_code == 404
