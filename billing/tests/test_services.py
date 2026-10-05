import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone

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
        return InitResult(provider_ref=self.ref, reference=f"subra-{order.pk}-t",
                          raw={"invoice": {"invoice_id": self.ref}})

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


@pytest.fixture(autouse=True)
def no_status_checks(monkeypatch):
    monkeypatch.setattr(services, "schedule_status_check", lambda payment: None)


@pytest.fixture
def ready(order_factory):
    """A generated, unpaid KES 350 deck (payment rules don't need its files)."""
    return order_factory(status=S.READY)


def pending_payment(order, ref="INV-1"):
    return services.start_payment(order, "0712 345 678", provider=FakeProvider(ref=ref))


def callback(**kw):
    return services.handle_callback("intasend", json.dumps(kw).encode(), {}, provider=FakeProvider())


# --- starting a payment -----------------------------------------------------

def test_start_payment_sends_stk_push_and_waits(ready):
    provider = FakeProvider()
    payment = services.start_payment(ready, "0712 345 678", provider=provider)
    assert provider.initiated == [(ready.pk, "254712345678", 350)]
    assert (payment.provider, payment.provider_ref, payment.reference) == ("intasend", "INV-1", f"subra-{ready.pk}-t")
    assert (payment.status, payment.amount_kes) == ("pending", 350)
    ready.refresh_from_db()
    assert ready.status == S.READY and not ready.is_paid
    assert ready.events.last().note == "M-Pesa prompt sent to 254712345678 via intasend"
    assert ready.user.mpesa_phone == "254712345678"


def test_start_payment_rejects_bad_phone(ready):
    with pytest.raises(services.PaymentError):
        services.start_payment(ready, "12345", provider=FakeProvider())


@pytest.mark.parametrize("status", [S.QUOTED, S.QUEUED, S.PROCESSING, S.RERENDERING, S.FAILED, S.EXPIRED])
def test_only_a_ready_deck_can_be_paid_for(order_factory, status):
    with pytest.raises(services.PaymentError):
        pending_payment(order_factory(status=status))


def test_a_paid_deck_cant_be_paid_for_again(order_factory):
    with pytest.raises(services.PaymentError):
        pending_payment(order_factory(status=S.READY, paid_at=timezone.now()))


def test_one_payment_at_a_time(ready):
    pending_payment(ready)
    with pytest.raises(services.PaymentError):
        pending_payment(ready, ref="INV-2")


def test_failed_push_creates_no_payment(ready):
    with pytest.raises(services.PaymentError):
        services.start_payment(ready, "0712345678", provider=FakeProvider(fail=True))
    assert not Payment.objects.exists()


# --- callbacks --------------------------------------------------------------

def test_paid_callback_unlocks_the_download_for_7_days(ready):
    pending_payment(ready)
    payment = callback(ref="INV-1")
    assert (payment.status, payment.provider_receipt) == ("paid", "SJK1ABC2DE")
    assert payment.fee_kes == Decimal("15.75") and not payment.fee_estimated
    assert payment.raw_callbacks == [{"state": "paid", "ref": "INV-1"}]
    ready.refresh_from_db()
    assert ready.is_paid and ready.status == S.READY
    assert timedelta(days=6, hours=23) < ready.expires_at - timezone.now() <= timedelta(days=7)
    assert ready.events.last().note == "Paid, M-Pesa receipt SJK1ABC2DE"


def test_missing_fee_is_estimated(ready, settings):
    settings.PAYMENT_FEE_RATE = 0.015
    pending_payment(ready)
    payment = callback(ref="INV-1", fee="0.00")
    assert payment.fee_kes == Decimal("5.25") and payment.fee_estimated


def test_duplicate_callback_changes_nothing(ready):
    pending_payment(ready)
    callback(ref="INV-1")
    ready.refresh_from_db()
    paid_at = ready.paid_at
    payment = callback(ref="INV-1", receipt="OTHER")
    assert payment.provider_receipt == "SJK1ABC2DE"
    assert payment.callback_count == 2 and len(payment.raw_callbacks) == 2
    ready.refresh_from_db()
    assert ready.paid_at == paid_at
    assert ready.events.filter(note__startswith="Paid").count() == 1
    assert mail.outbox == []


def test_pending_callback_changes_nothing(ready):
    pending_payment(ready)
    assert callback(ref="INV-1", state="pending").status == "pending"
    ready.refresh_from_db()
    assert not ready.is_paid


def test_failed_callback_leaves_the_deck_ready_to_pay_again(ready):
    pending_payment(ready)
    payment = callback(ref="INV-1", state="failed", code="1032", reason="Request cancelled by user")
    assert payment.status == "failed" and payment.result_code == "1032"
    ready.refresh_from_db()
    assert ready.status == S.READY and not ready.is_paid and ready.can_pay


def test_amount_mismatch_is_not_paid_and_alerts_admin(ready):
    pending_payment(ready)
    payment = callback(ref="INV-1", amount="1")
    assert payment.status == "failed"
    ready.refresh_from_db()
    assert not ready.is_paid
    assert "amount" in mail.outbox[0].body.lower()


def test_paid_event_without_an_amount_is_not_trusted(ready):
    pending_payment(ready)
    assert callback(ref="INV-1", amount=None).status == "pending"


def test_reference_mismatch_is_ignored(ready):
    pending_payment(ready)
    assert callback(ref="INV-1", reference="subra-999-x").status == "pending"


def test_unknown_ref_is_ignored(db):
    assert callback(ref="nope") is None


def test_late_success_after_a_timeout_still_pays(ready):
    payment = pending_payment(ready)
    services.check_payment(payment, provider=FakeProvider(status=event(state="failed", code="1037", reason="timeout")))
    payment = callback(ref="INV-1")
    ready.refresh_from_db()
    assert payment.status == "paid" and ready.is_paid


def test_second_successful_payment_is_flagged_for_refund(ready):
    first = pending_payment(ready, ref="INV-1")
    services.check_payment(first, provider=FakeProvider(status=event(state="failed", code="1037")))
    services.start_payment(ready, "0712345678", provider=FakeProvider(ref="INV-2"))
    callback(ref="INV-2", receipt="R2")
    dup = callback(ref="INV-1", receipt="R1")
    assert dup.status == "paid" and "refund" in dup.result_desc.lower()
    assert any("Duplicate" in m.subject and "refund" in m.subject.lower() for m in mail.outbox)


def test_payment_for_a_deleted_deck_is_flagged_for_refund(ready):
    pending_payment(ready)
    Order.objects.filter(pk=ready.pk).update(status=S.EXPIRED, files_deleted_at=timezone.now())
    payment = callback(ref="INV-1")
    assert payment.status == "paid" and "refund" in payment.result_desc.lower()
    ready.refresh_from_db()
    assert not ready.is_paid
    assert any("deleted deck" in m.subject for m in mail.outbox)


# --- status checks ----------------------------------------------------------

def test_status_check_pending_changes_nothing(ready):
    payment = pending_payment(ready)
    assert services.check_payment(payment, provider=FakeProvider(status=event(state="pending"))) == "pending"
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.status_checks == 1 and payment.callback_count == 0


def test_status_check_success_marks_paid(ready):
    payment = pending_payment(ready)
    assert services.check_payment(payment, provider=FakeProvider(status=event())) == "paid"
    ready.refresh_from_db()
    assert ready.is_paid


def test_status_check_outage_keeps_waiting(ready):
    payment = pending_payment(ready)
    assert services.check_payment(payment, provider=FakeProvider(status=ProviderError("down"))) == "pending"


# --- IntaSend webhook endpoint ----------------------------------------------

def intasend_payload(payment, **extra):
    data = {"invoice_id": payment.provider_ref, "state": "COMPLETE", "provider": "M-PESA", "charges": "15.75",
            "net_amount": "334.25", "currency": "KES", "value": "350.00", "api_ref": payment.reference,
            "mpesa_reference": "SJK1ABC2DE", "failed_reason": None, "failed_code": None,
            "challenge": "challenge-123"}
    data.update(extra)
    return data


def test_webhook_rejects_a_wrong_challenge(client, ready):
    payment = pending_payment(ready)
    resp = client.post("/billing/webhooks/intasend/", data=json.dumps(intasend_payload(payment, challenge="x")),
                       content_type="application/json")
    assert resp.status_code == 403
    payment.refresh_from_db()
    assert payment.status == "pending" and payment.raw_callbacks == []


def test_webhook_marks_paid(client, ready):
    payment = pending_payment(ready)
    resp = client.post("/billing/webhooks/intasend/", data=json.dumps(intasend_payload(payment)),
                       content_type="application/json")
    assert resp.status_code == 200
    payment.refresh_from_db()
    assert (payment.status, payment.provider_receipt, payment.fee_kes) == ("paid", "SJK1ABC2DE", Decimal("15.75"))
    ready.refresh_from_db()
    assert ready.is_paid


def test_webhook_rejects_malformed_json(client, db):
    resp = client.post("/billing/webhooks/intasend/", data="{not json", content_type="application/json")
    assert resp.status_code == 400


def test_webhook_for_an_unknown_provider_is_404(client, db):
    assert client.post("/billing/webhooks/stripe/", data="{}", content_type="application/json").status_code == 404


# --- a charge the provider accepted after our request timed out --------------

def test_a_paid_charge_we_have_no_record_of_alerts_admins(db):
    """Paystack can accept a charge after our request timed out: no Payment row exists, but the customer paid."""
    assert callback(ref="subra-77-ab12cd34", reference="subra-77-ab12cd34", receipt="QJK1ABC2DE") is None
    alert = next(m for m in mail.outbox if "no record" in m.subject.lower())
    assert "order 77" in alert.body and "QJK1ABC2DE" in alert.body


def test_a_charge_from_elsewhere_on_the_account_sends_no_alert(db):
    callback(ref="someone-else-1")
    assert mail.outbox == []


def test_an_unrecorded_charge_that_failed_sends_no_alert(db):
    callback(ref="subra-77-ab12cd34", reference="subra-77-ab12cd34", state="failed")
    assert mail.outbox == []


def test_a_retried_paid_callback_doesnt_undo_a_refund(ready):
    """Providers retry webhooks for days; a payment already refunded must stay refunded, with no new alert."""
    pending_payment(ready)
    callback(ref="INV-1")
    Payment.objects.filter(provider_ref="INV-1").update(status="refunded", refunded_at=timezone.now())
    mail.outbox.clear()
    payment = callback(ref="INV-1")
    assert payment.status == "refunded" and payment.refunded_at
    assert mail.outbox == []
