from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from .models import Payment
from .services import check_payment, expire_payment

MAX_CHECKS = 6  # 60s after the push, then every 30s for ~3 minutes
GIVE_UP_AFTER = timedelta(minutes=30)


@shared_task
def check_payment_task(payment_id):
    """If no callback arrived, ask Daraja for the result; keep asking for a few minutes."""
    payment = Payment.objects.filter(pk=payment_id).first()
    if payment is None or payment.status != Payment.Status.PENDING:
        return
    if check_payment(payment) == "pending":
        payment.refresh_from_db()
        if payment.status_checks < MAX_CHECKS:
            check_payment_task.apply_async(args=[payment_id], countdown=30)


@shared_task
def sweep_pending_payments():
    """Safety net for lost tasks: re-check stale pending payments, and give up on very old ones."""
    now = timezone.now()
    for payment in Payment.objects.filter(status=Payment.Status.PENDING, created_at__lt=now - timedelta(minutes=2)):
        if check_payment(payment) == "pending" and payment.created_at < now - GIVE_UP_AFTER:
            expire_payment(payment)
