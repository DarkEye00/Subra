import json

import pytest
from django.core import mail

from billing import services
from billing.models import Payment
from billing.mpesa import MpesaError
from orders.models import Order

S = Order.Status


class FakeDaraja:
    def __init__(self, push=None, query=None):
        self.push_result = push or {"MerchantRequestID": "m-1", "CheckoutRequestID": "ws_CO_1", "ResponseCode": "0"}
        self.query_result = query
        self.pushes = []

    def stk_push(self, phone, amount, reference, description):
        self.pushes.append((phone, amount, reference, description))
        if isinstance(self.push_result, Exception):
            raise self.push_result
        return self.push_result

    def query(self, checkout_request_id):
        return self.query_result


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "enqueue_processing", lambda order: calls.append(order.pk))
    monkeypatch.setattr(services, "schedule_status_check", lambda payment: None)
    return calls


def callback(checkout_id="ws_CO_1", code=0, amount=350, receipt="SJK1ABC2DE", desc="ok"):
    stk = {"MerchantRequestID": "m-1", "CheckoutRequestID": checkout_id, "ResultCode": code, "ResultDesc": desc}
    if code == 0:
        stk["CallbackMetadata"] = {"Item": [
            {"Name": "Amount", "Value": amount}, {"Name": "MpesaReceiptNumber", "Value": receipt},
            {"Name": "TransactionDate", "Value": 20260930101500}, {"Name": "PhoneNumber", "Value": 254712345678},
        ]}
    return {"Body": {"stkCallback": stk}}


def pending_payment(order, enqueued):
    return services.start_payment(order, "0712 345 678", client=FakeDaraja())


# --- starting a payment ---------------------------------------------------

def test_start_payment_sends_stk_push_and_waits(quoted_order, enqueued):
    daraja = FakeDaraja()
    payment = services.start_payment(quoted_order, "0712 345 678", client=daraja)
    assert daraja.pushes[0][:2] == ("254712345678", 350)
    assert (payment.status, payment.checkout_request_id, payment.amount_kes) == ("pending", "ws_CO_1", 350)
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_PENDING
    assert quoted_order.user.mpesa_phone == "254712345678"


def test_start_payment_rejects_bad_phone(quoted_order, enqueued):
    with pytest.raises(services.PaymentError):
        services.start_payment(quoted_order, "12345", client=FakeDaraja())


def test_start_payment_needs_a_payable_order(quoted_order, enqueued):
    pending_payment(quoted_order, enqueued)
    with pytest.raises(services.PaymentError):
        services.start_payment(quoted_order, "0712345678", client=FakeDaraja())


def test_failed_stk_push_leaves_order_quoted(quoted_order, enqueued):
    with pytest.raises(services.PaymentError):
        services.start_payment(quoted_order, "0712345678", client=FakeDaraja(push=MpesaError("down")))
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.QUOTED
    assert not Payment.objects.exists()


# --- callbacks --------------------------------------------------------------

def test_success_callback_marks_paid_and_starts_processing(quoted_order, enqueued, django_capture_on_commit_callbacks):
    pending_payment(quoted_order, enqueued)
    with django_capture_on_commit_callbacks(execute=True):
        payment = services.handle_callback(callback())
    assert (payment.status, payment.mpesa_receipt) == ("paid", "SJK1ABC2DE")
    assert payment.raw_callback == callback()
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAID
    assert enqueued == [quoted_order.pk]


def test_duplicate_callback_changes_nothing(quoted_order, enqueued, django_capture_on_commit_callbacks):
    pending_payment(quoted_order, enqueued)
    with django_capture_on_commit_callbacks(execute=True):
        services.handle_callback(callback())
        payment = services.handle_callback(callback(receipt="OTHER"))
    assert payment.mpesa_receipt == "SJK1ABC2DE"
    assert payment.callback_count == 2
    assert enqueued == [quoted_order.pk]
    assert quoted_order.events.filter(to_status=S.PAID).count() == 1


def test_cancelled_payment_marks_order_payment_failed(quoted_order, enqueued):
    pending_payment(quoted_order, enqueued)
    payment = services.handle_callback(callback(code=1032, desc="Request cancelled by user"))
    assert payment.status == "failed" and payment.result_code == "1032"
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_FAILED
    assert enqueued == []


def test_amount_mismatch_is_not_paid_and_alerts_admin(quoted_order, enqueued):
    pending_payment(quoted_order, enqueued)
    payment = services.handle_callback(callback(amount=1))
    assert payment.status == "failed"
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_FAILED
    assert "amount" in mail.outbox[0].body.lower()


def test_unknown_checkout_id_is_ignored(quoted_order, enqueued):
    assert services.handle_callback(callback(checkout_id="nope")) is None


def test_late_success_after_timeout_still_pays(quoted_order, enqueued, django_capture_on_commit_callbacks):
    payment = pending_payment(quoted_order, enqueued)
    services.check_payment(payment, client=FakeDaraja(query={"ResultCode": "1037", "ResultDesc": "timeout"}))
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAYMENT_FAILED
    with django_capture_on_commit_callbacks(execute=True):
        payment = services.handle_callback(callback())
    quoted_order.refresh_from_db()
    assert payment.status == "paid" and quoted_order.status == S.PAID
    assert enqueued == [quoted_order.pk]


def test_second_successful_payment_is_flagged_for_refund(quoted_order, enqueued, django_capture_on_commit_callbacks):
    first = pending_payment(quoted_order, enqueued)
    services.check_payment(first, client=FakeDaraja(query={"ResultCode": "1037", "ResultDesc": "timeout"}))
    services.start_payment(quoted_order, "0712345678",
                           client=FakeDaraja(push={"MerchantRequestID": "m-2", "CheckoutRequestID": "ws_CO_2", "ResponseCode": "0"}))
    with django_capture_on_commit_callbacks(execute=True):
        services.handle_callback(callback(checkout_id="ws_CO_2", receipt="R2"))
        dup = services.handle_callback(callback(checkout_id="ws_CO_1", receipt="R1"))
    assert dup.status == "paid" and "refund" in dup.result_desc.lower()
    assert enqueued == [quoted_order.pk]
    assert any("refund" in m.subject.lower() for m in mail.outbox)


# --- status query -----------------------------------------------------------

def test_status_query_pending_changes_nothing(quoted_order, enqueued):
    payment = pending_payment(quoted_order, enqueued)
    assert services.check_payment(payment, client=FakeDaraja(query={"pending": True})) == "pending"
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.status_checks == 1


def test_status_query_success_marks_paid(quoted_order, enqueued, django_capture_on_commit_callbacks):
    payment = pending_payment(quoted_order, enqueued)
    with django_capture_on_commit_callbacks(execute=True):
        assert services.check_payment(payment, client=FakeDaraja(query={"ResultCode": "0", "ResultDesc": "ok"})) == "paid"
    quoted_order.refresh_from_db()
    assert quoted_order.status == S.PAID


# --- callback endpoint ------------------------------------------------------

def test_callback_view_requires_the_secret(client, quoted_order, enqueued):
    pending_payment(quoted_order, enqueued)
    resp = client.post("/billing/mpesa/callback/wrong/", data=json.dumps(callback()), content_type="application/json")
    assert resp.status_code == 404
    assert Payment.objects.get().status == "pending"


def test_callback_view_accepts_valid_callback(client, quoted_order, enqueued):
    pending_payment(quoted_order, enqueued)
    resp = client.post("/billing/mpesa/callback/cb-secret/", data=json.dumps(callback()),
                       content_type="application/json")
    assert resp.status_code == 200 and resp.json()["ResultCode"] == 0
    assert Payment.objects.get().status == "paid"


def test_callback_view_rejects_malformed_json(client, db):
    resp = client.post("/billing/mpesa/callback/cb-secret/", data="{not json", content_type="application/json")
    assert resp.status_code == 400
