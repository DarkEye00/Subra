import logging
from datetime import timedelta

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from billing.models import Payment
from polisher.pipeline import PlannerUnavailable

from . import processing, spend
from .models import Order

log = logging.getLogger(__name__)
S = Order.Status
IN_FLIGHT = set(Order.IN_FLIGHT)  # never expired: work is waiting or running
STALLED_AFTER = timedelta(minutes=30)  # well past the slowest generation (three Claude attempts, then previews)
RESTARTED = "Restarted: the worker stopped mid-job"


JOB_LIMITS = {"soft_time_limit": settings.JOB_TIME_LIMIT_SECONDS, "time_limit": settings.JOB_TIME_LIMIT_SECONDS + 60}


@shared_task(bind=True, max_retries=12, **JOB_LIMITS)
def process_order(self, order_id):
    with transaction.atomic():
        order = Order.objects.select_for_update().filter(pk=order_id).first()
        if order is None or order.status != S.QUEUED:
            return "skipped"
        if spend.cap_reached():
            order.waiting_for_capacity = True
            order.save(update_fields=["waiting_for_capacity", "updated_at"])
            try:
                spend.ensure_capacity()  # sends the once-a-day alert
            except spend.SpendCapReached:
                pass
            return "queued"
        order.transition(S.PROCESSING, note="Generating", waiting_for_capacity=False)

    try:
        result = processing.run_polish(order)
    except spend.SpendCapReached:
        order.transition(S.QUEUED, note="Daily AI spend cap reached mid-job", waiting_for_capacity=True)
        return "queued"
    except PlannerUnavailable as e:
        order.transition(S.QUEUED, note=f"AI provider unavailable: {e}"[:500], waiting_for_capacity=True)
        if self.request.called_directly or self.request.is_eager:
            return "deferred"
        raise self.retry(exc=e, countdown=300)
    except SoftTimeLimitExceeded:
        order.refresh_from_db()
        processing.fail(order, f"Timed out after {settings.JOB_TIME_LIMIT_SECONDS} seconds")
        return "failed"
    except Exception as e:
        log.exception("Processing crashed for order %s", order_id)
        order.refresh_from_db()
        processing.fail(order, f"Internal error: {type(e).__name__}: {e}")
        return "failed"

    if not result.ok:
        processing.fail(order, result.failure or "Verification failed")
        return "failed"
    return "done"


@shared_task(**JOB_LIMITS)
def rerender_order(order_id):
    order = Order.objects.select_related("switching_to").filter(pk=order_id).first()
    if order is None or order.status != S.RERENDERING or order.switching_to is None:
        return "skipped"
    try:
        return "done" if processing.run_rerender(order) else "unchanged"
    except Exception as e:
        log.exception("Theme switch crashed for order %s", order_id)
        processing.finish_switch_failed(order, order.switching_to, f"Internal error: {type(e).__name__}: {e}")
        return "failed"


def _release_stalled_generations():
    """A worker killed mid-job (a deploy, running out of memory) leaves the order PROCESSING for ever, and an
    unpaid deck blocks the customer's next one. Restart it once; if it stalls again, fail it."""
    cutoff = timezone.now() - STALLED_AFTER
    restarted = []
    for order in Order.objects.filter(status=S.PROCESSING, updated_at__lt=cutoff):
        last_attempt = order.ai_jobs.order_by("-created_at").values_list("created_at", flat=True).first()
        if last_attempt and last_attempt >= cutoff:
            continue  # still making progress
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=order.pk)
            if order.status != S.PROCESSING:
                continue
            if order.events.filter(note=RESTARTED).exists():
                processing.fail(order, "The worker stopped twice while polishing this deck")
            else:
                order.transition(S.QUEUED, note=RESTARTED, waiting_for_capacity=False)
                restarted.append(order.pk)
    for pk in restarted:
        process_order.delay(pk)


@shared_task
def release_queued_orders():
    """Restart stalled generations, release theme switches stuck for 10 minutes, then start queued orders that
    were waiting (spend cap, AI outage) or whose task was lost."""
    _release_stalled_generations()
    stale = timezone.now() - timedelta(minutes=10)
    for order in Order.objects.filter(status=S.RERENDERING, updated_at__lt=stale).select_related("switching_to"):
        processing.finish_switch_failed(order, order.switching_to, "Timed out")
    if spend.cap_reached():
        return 0
    waiting = Q(waiting_for_capacity=True) | Q(updated_at__lt=stale)
    ids = list(Order.objects.filter(waiting, status=S.QUEUED).values_list("pk", flat=True))
    for pk in ids:
        process_order.delay(pk)
    return len(ids)


def _expired():
    """Decks whose files are due for deletion now."""
    return (
        Order.objects.filter(expires_at__lte=timezone.now(), files_deleted_at__isnull=True)
        .exclude(status__in=IN_FLIGHT)
        .exclude(payments__status=Payment.Status.PENDING)  # wait for the payment sweep to settle it
        .distinct()
    )


def _expired_ids():
    return list(_expired().values_list("pk", flat=True))


@shared_task
def delete_expired_files():
    """Delete originals, results and previews once expires_at passes (48 hours unpaid, 7 days after payment)."""
    count = 0
    for pk in _expired_ids():
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=pk)
            # the list was read before the lock: a payment or a theme switch may have started since
            if not _expired().filter(pk=pk).exists():
                continue
            processing.delete_order_files(order)
            if order.can_transition(S.EXPIRED):
                order.transition(S.EXPIRED, note="Files deleted after retention period")
            else:
                order.save()
        count += 1
    return count
