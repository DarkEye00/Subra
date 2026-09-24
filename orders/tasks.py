import logging
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from polisher.pipeline import PlannerUnavailable

from . import processing, spend
from .models import Order

log = logging.getLogger(__name__)
S = Order.Status
ACTIVE = {S.PAYMENT_PENDING, S.PAID, S.PROCESSING}


@shared_task(bind=True, max_retries=12)
def process_order(self, order_id):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None or order.status != S.PAID:
            return "skipped"
        if spend.cap_reached():
            order.waiting_for_capacity = True
            order.save(update_fields=["waiting_for_capacity", "updated_at"])
            try:
                spend.ensure_capacity()  # sends the once-a-day alert
            except spend.SpendCapReached:
                pass
            return "queued"
        run = 2 if order.rerun_used else 1
        order.transition(S.PROCESSING, note=f"run {run}", waiting_for_capacity=False)

    try:
        result = processing.run_polish(order, run)
    except spend.SpendCapReached:
        order.transition(S.PAID, note="Daily AI spend cap reached mid-job", waiting_for_capacity=True)
        return "queued"
    except PlannerUnavailable as e:
        order.transition(S.PAID, note=f"AI provider unavailable: {e}"[:500], waiting_for_capacity=True)
        if self.request.called_directly or self.request.is_eager:
            return "deferred"
        raise self.retry(exc=e, countdown=300)
    except Exception as e:
        log.exception("Processing crashed for order %s", order_id)
        order.refresh_from_db()
        processing.fail(order, run, f"Internal error: {type(e).__name__}: {e}")
        return "failed"

    if not result.ok:
        processing.fail(order, run, result.failure or "Verification failed")
        return "failed"
    return "done"


@shared_task
def release_queued_orders():
    """Start paid orders that were waiting (spend cap, AI outage) or whose task was lost."""
    if spend.cap_reached():
        return 0
    stale = timezone.now() - timedelta(minutes=10)
    waiting = Q(waiting_for_capacity=True) | Q(updated_at__lt=stale)
    ids = list(Order.objects.filter(waiting, status=S.PAID).values_list("pk", flat=True))
    for pk in ids:
        process_order.delay(pk)
    return len(ids)


@shared_task
def delete_expired_files():
    """Delete originals, results, and previews after FILE_RETENTION_DAYS."""
    count = 0
    expired = Order.objects.filter(expires_at__lte=timezone.now(), files_deleted_at__isnull=True).exclude(
        status__in=ACTIVE
    )
    for order in expired:
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            processing.delete_order_files(order)
            if order.can_transition(S.EXPIRED):
                order.transition(S.EXPIRED, note="Files deleted after retention period")
            else:
                order.save()
        count += 1
    return count
