"""Payment flow, the same for every provider: start, apply events idempotently, check status.

Only a confirmed payment of the exact quoted amount moves an order to PAID.
"""

import logging
from decimal import Decimal

from django.conf import settings
from django.db import transaction

from core.alerts import alert_admins
from orders.models import Order

from .models import Payment
from .phone import normalize_phone
from .providers import PaymentEvent, ProviderError, get_provider

log = logging.getLogger(__name__)
S = Order.Status
PAYABLE = {S.QUOTED, S.PAYMENT_FAILED}
MAX_STORED_CALLBACKS = 20


class PaymentError(Exception):
    """Something the customer should see, e.g. an invalid phone number."""


def enqueue_processing(order):
    from orders.tasks import process_order

    process_order.delay(order.pk)


def schedule_status_check(payment):
    from .tasks import check_payment_task

    check_payment_task.apply_async(args=[payment.pk], countdown=60)


def start_payment(order, raw_phone, provider=None):
    try:
        phone = normalize_phone(raw_phone)
    except ValueError as e:
        raise PaymentError(str(e))
    provider = provider or get_provider()
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status not in PAYABLE:
            raise PaymentError("This order can't be paid right now.")
        try:
            result = provider.initiate(order, phone)
        except ProviderError as e:
            log.warning("Payment request failed for order %s: %s", order.pk, e)
            raise PaymentError("We couldn't reach M-Pesa. Please try again in a minute.")
        payment = Payment.objects.create(
            order=order, provider=provider.name, provider_ref=result.provider_ref, reference=result.reference,
            phone=phone, amount_kes=order.price_kes, raw_init=result.raw,
        )
        order.transition(S.PAYMENT_PENDING, note=f"M-Pesa prompt sent to {phone} via {provider.name}")
        order.user.mpesa_phone = phone
        order.user.save(update_fields=["mpesa_phone"])
        transaction.on_commit(lambda: schedule_status_check(payment))
    return payment


def handle_callback(provider_name, body, headers, provider=None):
    """Authenticate and apply a webhook. Raises CallbackRejected (not genuine) or ValueError (malformed)."""
    provider = provider or get_provider(provider_name)
    return apply_event(provider.name, provider.handle_callback(body, headers), source="callback")


def check_payment(payment, provider=None):
    """Ask the provider about a pending payment. Returns the payment's status afterwards."""
    if payment.status != Payment.Status.PENDING:
        return payment.status
    provider = provider or get_provider(payment.provider)
    try:
        event = provider.check_status(payment.provider_ref)
    except ProviderError as e:
        log.warning("Status check failed for payment %s: %s", payment.pk, e)
        return Payment.Status.PENDING
    Payment.objects.filter(pk=payment.pk).update(status_checks=payment.status_checks + 1)
    result = apply_event(payment.provider, event, source="status")
    return result.status if result else Payment.Status.PENDING


def expire_payment(payment, reason="No confirmation from M-Pesa"):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == Payment.Status.PENDING:
            _mark_failed(payment, "timeout", reason)


def apply_event(provider_name, event: PaymentEvent, source):
    """Apply a normalised provider event. Safe to call any number of times with the same event."""
    with transaction.atomic():
        payment = (
            Payment.objects.select_for_update()
            .filter(provider=provider_name, provider_ref=event.provider_ref)
            .first()
        )
        if payment is None:
            log.warning("%s event for unknown payment %s", provider_name, event.provider_ref)
            return None
        if source == "callback":
            payment.callback_count += 1
            payment.raw_callbacks = (payment.raw_callbacks + [event.raw])[-MAX_STORED_CALLBACKS:]
            payment.save(update_fields=["callback_count", "raw_callbacks", "updated_at"])
        if event.reference and payment.reference and event.reference != payment.reference:
            log.warning("Reference mismatch on payment %s: got %s", payment.pk, event.reference)
            return payment

        if event.state == "pending":
            return payment
        if event.state == "failed":
            if payment.status == Payment.Status.PENDING:
                _mark_failed(payment, event.code, event.reason or event.code)
            if event.amount:  # e.g. a partial payment: money moved, so a human must look
                alert_admins(
                    f"Partial or failed payment with money on order {payment.order_id}",
                    f"Payment {payment.pk} from {payment.phone} ended as {event.code} with amount KES {event.amount} "
                    f"(quoted KES {payment.amount_kes}). Check the provider dashboard and refund if needed.",
                )
            return payment

        # state == "paid"
        if payment.status == Payment.Status.PAID:
            return payment  # duplicate
        if event.amount is None:
            log.warning("Paid event without an amount for payment %s; waiting for confirmation", payment.pk)
            return payment
        if event.amount != Decimal(payment.amount_kes):
            _mark_failed(payment, event.code or "amount", f"Amount mismatch: paid {event.amount}, expected {payment.amount_kes}")
            alert_admins(
                f"M-Pesa amount mismatch on order {payment.order_id}",
                f"Payment {payment.pk} from {payment.phone} paid KES {event.amount}, but the quoted amount is "
                f"KES {payment.amount_kes}. The order was not marked paid. Check and refund if needed.",
            )
            return payment
        _mark_paid(payment, event)
    return payment


def _fee(payment, event):
    if event.fee:
        return event.fee, False
    estimate = (Decimal(payment.amount_kes) * Decimal(str(settings.PAYMENT_FEE_RATE))).quantize(Decimal("0.01"))
    return estimate, True


def _mark_failed(payment, code, desc):
    payment.status = Payment.Status.FAILED
    payment.result_code, payment.result_desc = (code or "")[:32], (desc or "")[:255]
    payment.save()
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    has_other_pending = order.payments.filter(status=Payment.Status.PENDING).exclude(pk=payment.pk).exists()
    if order.status == S.PAYMENT_PENDING and not has_other_pending:
        order.transition(S.PAYMENT_FAILED, note=f"Payment failed: {code} {desc}"[:500])


def _mark_paid(payment, event):
    payment.status = Payment.Status.PAID
    payment.result_code, payment.result_desc = (event.code or "")[:32], (event.reason or "")[:255]
    if event.receipt:
        payment.provider_receipt = event.receipt
    payment.fee_kes, payment.fee_estimated = _fee(payment, event)
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    if order.status == S.PAYMENT_FAILED:
        order.transition(S.PAYMENT_PENDING, note="Late M-Pesa confirmation")
    if order.status == S.PAYMENT_PENDING:
        payment.save()
        order.transition(S.PAID, note=f"M-Pesa receipt {event.receipt or '(from status check)'}")
        transaction.on_commit(lambda: enqueue_processing(order))
        return
    # The order was already paid by another payment: this one must be refunded.
    payment.result_desc = f"Duplicate payment, refund needed. {event.reason}"[:255]
    payment.save()
    alert_admins(
        f"Duplicate M-Pesa payment on order {order.pk}: refund needed",
        f"Payment {payment.pk} from {payment.phone} (receipt {event.receipt or 'unknown'}, KES {payment.amount_kes}) "
        f"arrived for order {order.pk}, which is already {order.status}. Refund it from the provider dashboard.",
    )
