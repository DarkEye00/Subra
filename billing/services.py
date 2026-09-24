"""M-Pesa payment flow: STK push, idempotent callbacks, and status queries.

Only a confirmed payment of the exact quoted amount moves an order to PAID.
"""

import logging

from django.db import transaction

from core.alerts import alert_admins
from orders.models import Order

from .models import Payment
from .mpesa import DarajaClient, MpesaError, normalize_phone

log = logging.getLogger(__name__)
S = Order.Status
PAYABLE = {S.QUOTED, S.PAYMENT_FAILED}


class PaymentError(Exception):
    """Something the customer should see, e.g. an invalid phone number."""


def enqueue_processing(order):
    from orders.tasks import process_order

    process_order.delay(order.pk)


def schedule_status_check(payment):
    from .tasks import check_payment_task

    check_payment_task.apply_async(args=[payment.pk], countdown=60)


def start_payment(order, raw_phone, client=None):
    try:
        phone = normalize_phone(raw_phone)
    except ValueError as e:
        raise PaymentError(str(e))
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.status not in PAYABLE:
            raise PaymentError("This order can't be paid right now.")
        client = client or DarajaClient.from_settings()
        try:
            result = client.stk_push(
                phone, order.price_kes, reference=f"SUBRA{order.pk}", description="Deck polish"
            )
        except MpesaError as e:
            log.warning("STK push failed for order %s: %s", order.pk, e)
            raise PaymentError("We couldn't reach M-Pesa. Please try again in a minute.")
        payment = Payment.objects.create(
            order=order,
            phone=phone,
            amount_kes=order.price_kes,
            merchant_request_id=result.get("MerchantRequestID", ""),
            checkout_request_id=result["CheckoutRequestID"],
        )
        order.transition(S.PAYMENT_PENDING, note=f"STK push to {phone}")
        order.user.mpesa_phone = phone
        order.user.save(update_fields=["mpesa_phone"])
        transaction.on_commit(lambda: schedule_status_check(payment))
    return payment


def handle_callback(payload):
    """Process a Daraja STK callback. Safe to call any number of times with the same payload."""
    stk = payload["Body"]["stkCallback"]
    checkout_id = stk["CheckoutRequestID"]
    with transaction.atomic():
        payment = Payment.objects.select_for_update().filter(checkout_request_id=checkout_id).first()
        if payment is None:
            log.warning("Callback for unknown CheckoutRequestID %s", checkout_id)
            return None
        payment.callback_count += 1
        if payment.raw_callback is None:
            payment.raw_callback = payload
        payment.save(update_fields=["callback_count", "raw_callback", "updated_at"])
        code, desc = str(stk.get("ResultCode")), stk.get("ResultDesc", "")

        if code != "0":
            if payment.status == Payment.Status.PENDING:
                _mark_failed(payment, code, desc)
            return payment
        if payment.status == Payment.Status.PAID:
            return payment  # duplicate success callback

        items = {i.get("Name"): i.get("Value") for i in stk.get("CallbackMetadata", {}).get("Item", [])}
        amount = items.get("Amount")
        if amount is None or round(float(amount)) != payment.amount_kes:
            _mark_failed(payment, code, f"Amount mismatch: paid {amount}, expected {payment.amount_kes}")
            alert_admins(
                f"M-Pesa amount mismatch on order {payment.order_id}",
                f"Payment {payment.pk} from {payment.phone} paid KES {amount}, but the quoted amount is "
                f"KES {payment.amount_kes}. The order was not marked paid. Check and refund if needed.",
            )
            return payment
        _mark_paid(payment, receipt=str(items.get("MpesaReceiptNumber") or ""), code=code, desc=desc)
    return payment


def check_payment(payment, client=None):
    """Ask Daraja about a pending payment. Returns "pending", "paid", "failed", or the current status."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status != Payment.Status.PENDING:
            return payment.status
        client = client or DarajaClient.from_settings()
        try:
            result = client.query(payment.checkout_request_id)
        except MpesaError as e:
            log.warning("STK query failed for payment %s: %s", payment.pk, e)
            return "pending"
        payment.status_checks += 1
        payment.save(update_fields=["status_checks", "updated_at"])
        if result.get("pending"):
            return "pending"
        code, desc = result["ResultCode"], result.get("ResultDesc", "")
        if code == "0":
            _mark_paid(payment, receipt="", code=code, desc=desc)
            return "paid"
        _mark_failed(payment, code, desc)
        return "failed"


def expire_payment(payment, reason="No confirmation from M-Pesa"):
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == Payment.Status.PENDING:
            _mark_failed(payment, "timeout", reason)


def _mark_failed(payment, code, desc):
    payment.status = Payment.Status.FAILED
    payment.result_code, payment.result_desc = code, desc[:255]
    payment.save()
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    has_other_pending = order.payments.filter(status=Payment.Status.PENDING).exclude(pk=payment.pk).exists()
    if order.status == S.PAYMENT_PENDING and not has_other_pending:
        order.transition(S.PAYMENT_FAILED, note=f"M-Pesa {code}: {desc}"[:500])


def _mark_paid(payment, receipt, code, desc):
    payment.status = Payment.Status.PAID
    payment.result_code, payment.result_desc = code, desc[:255]
    if receipt:
        payment.mpesa_receipt = receipt
    order = Order.objects.select_for_update().get(pk=payment.order_id)
    if order.status == S.PAYMENT_FAILED:
        order.transition(S.PAYMENT_PENDING, note="Late M-Pesa confirmation")
    if order.status == S.PAYMENT_PENDING:
        payment.save()
        order.transition(S.PAID, note=f"M-Pesa receipt {receipt or '(from status query)'}")
        transaction.on_commit(lambda: enqueue_processing(order))
        return
    # The order was already paid by another payment: this one must be refunded.
    payment.result_desc = f"Duplicate payment, refund needed. {desc}"[:255]
    payment.save()
    alert_admins(
        f"Duplicate M-Pesa payment on order {order.pk}: refund needed",
        f"Payment {payment.pk} from {payment.phone} (receipt {receipt or 'unknown'}, KES {payment.amount_kes}) "
        f"arrived for order {order.pk}, which is already {order.status}. Refund it from the M-Pesa portal.",
    )
