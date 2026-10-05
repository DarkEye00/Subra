"""Payment flow, the same for every provider: start, apply events idempotently, check status.

Only a confirmed payment of the exact quoted amount moves an order to PAID.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from core.alerts import alert_admins
from orders.models import Order

from .models import Payment
from .phone import normalize_phone
from .providers import PaymentEvent, ProviderError, get_provider

log = logging.getLogger(__name__)
S = Order.Status
MAX_STORED_CALLBACKS = 20

STARTER_THRESHOLDS = (0.8, 0.95)


def _check_starter_limit(payment):
    """Paystack stops a Starter Business at a lifetime limit until it's registered. Warn before that."""
    limit = settings.PAYSTACK_STARTER_LIMIT_KES
    if payment.provider != "paystack" or not limit:
        return
    collected = Payment.objects.filter(provider="paystack",
                                       status__in=[Payment.Status.PAID, Payment.Status.REFUNDED])
    total = collected.aggregate(t=Sum("amount_kes"))["t"] or 0
    before = total - payment.amount_kes  # refunds don't lower the total, so each threshold is crossed once
    for share in STARTER_THRESHOLDS:
        if before < limit * share <= total:
            alert_admins(
                f"Paystack Starter limit {round(share * 100)}% used",
                f"Paystack has collected KES {total:,} of the KES {limit:,} Starter Business limit. Payments stop "
                "at the limit until the business is registered. Register it, upgrade the Paystack account, then "
                "set PAYSTACK_STARTER_LIMIT_KES=0.",
            )


class PaymentError(Exception):
    """Something the customer should see, e.g. an invalid phone number."""


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
        if not order.can_pay:
            raise PaymentError("This deck can't be paid for right now.")
        try:
            result = provider.initiate(order, phone)
        except ProviderError as e:
            log.warning("Payment request failed for order %s: %s", order.pk, e)
            raise PaymentError("We couldn't reach M-Pesa. Please try again in a minute.")
        payment = Payment.objects.create(
            order=order, provider=provider.name, provider_ref=result.provider_ref, reference=result.reference,
            phone=phone, amount_kes=order.price_kes, raw_init=result.raw,
        )
        order.log(f"M-Pesa prompt sent to {phone} via {provider.name}")
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
            if event.state == "paid":
                _alert_unrecorded(provider_name, event)
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
        if payment.status in (Payment.Status.PAID, Payment.Status.REFUNDED):
            return payment  # a duplicate, or a retry after the admin refunded it
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
        _check_starter_limit(payment)
    return payment


def _alert_unrecorded(provider_name, event):
    """Our request can time out after the provider accepted the charge: then there's no Payment row, but the
    customer paid. Charges made elsewhere on the same account don't carry our reference, so stay quiet."""
    reference = event.reference or event.provider_ref or ""
    if not reference.startswith("subra-"):
        return
    parts = reference.split("-")
    order_pk = parts[1] if len(parts) >= 3 else "unknown"
    alert_admins(
        f"Payment with no record for order {order_pk}: check and refund",
        f"{provider_name} confirmed a payment SUBRA has no record of (reference {reference}, KES {event.amount}, "
        f"receipt {event.receipt or 'unknown'}). The payment request probably timed out on our side after the "
        f"provider accepted it, so order {order_pk} is still unpaid. Refund it from the provider dashboard and "
        "ask the customer to pay again.",
    )


def _fee(payment, event):
    if event.fee:
        return event.fee, False
    estimate = (Decimal(payment.amount_kes) * Decimal(str(settings.PAYMENT_FEE_RATE))).quantize(Decimal("0.01"))
    return estimate, True


def _mark_failed(payment, code, desc):
    """Only the payment changes: the deck stays ready, and the customer can pay again."""
    payment.status = Payment.Status.FAILED
    payment.result_code, payment.result_desc = (code or "")[:32], (desc or "")[:255]
    payment.save()


def _mark_paid(payment, event):
    payment.status = Payment.Status.PAID
    payment.result_code, payment.result_desc = (event.code or "")[:32], (event.reason or "")[:255]
    if event.receipt:
        payment.provider_receipt = event.receipt
    payment.fee_kes, payment.fee_estimated = _fee(payment, event)
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    receipt = event.receipt or "unknown"
    if order.is_paid:
        payment.result_desc = f"Duplicate payment, refund needed. {event.reason}"[:255]
        payment.save()
        alert_admins(
            f"Duplicate M-Pesa payment on order {order.pk}: refund needed",
            f"Payment {payment.pk} from {payment.phone} (receipt {receipt}, KES {payment.amount_kes}) arrived for "
            f"order {order.pk}, which is already paid. Refund it from the provider dashboard, then mark the "
            "payment refunded in the admin.",
        )
        return
    if order.status not in (S.READY, S.RERENDERING) or order.files_deleted_at:
        payment.result_desc = f"Deck no longer exists, refund needed. {event.reason}"[:255]
        payment.save()
        alert_admins(
            f"Payment for a deleted deck on order {order.pk}: refund needed",
            f"Payment {payment.pk} from {payment.phone} (receipt {receipt}, KES {payment.amount_kes}) arrived for "
            f"order {order.pk}, which is {order.get_status_display().lower()} and has no deck to download. "
            "Refund it from the provider dashboard, then mark the payment refunded in the admin.",
        )
        return
    payment.save()
    now = timezone.now()
    order.paid_at = now
    order.expires_at = now + timedelta(days=settings.FILE_RETENTION_DAYS)
    order.save(update_fields=["paid_at", "expires_at", "updated_at"])
    order.log(f"Paid, M-Pesa receipt {event.receipt or '(from status check)'}")
