"""Run the polisher for an order: fetch files, call the pipeline, store results, log AI cost."""

import logging
import re
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

from core.alerts import alert_admins
from polisher.pipeline import polish, rerender
from polisher.preview import PreviewError, render_previews

from . import spend
from .models import AIJob, Order

log = logging.getLogger(__name__)
S = Order.Status


def build_planners():
    from polisher.planner import ClaudePlanner, HeuristicPlanner

    if settings.POLISHER_PLANNER == "heuristic":
        return HeuristicPlanner(), None
    from polisher.ai import AnthropicProvider

    provider = AnthropicProvider()
    return (
        ClaudePlanner(provider, settings.PLANNER_MODEL, effort=settings.PLANNER_EFFORT, pricing=settings.AI_PRICING),
        ClaudePlanner(provider, settings.ESCALATION_MODEL, effort=settings.ESCALATION_EFFORT,
                      pricing=settings.AI_PRICING),
    )


def usd_to_kes(usd):
    rate = Decimal(str(settings.USD_KES_RATE)) * (1 + Decimal(str(settings.CARD_FX_MARKUP)))
    return (Decimal(str(usd)) * rate).quantize(Decimal("0.01"))


def log_attempt(order, record):
    AIJob.objects.create(
        order=order, attempt_number=record.number, planner=record.planner, model=record.model or "",
        input_tokens=record.usage.input_tokens, output_tokens=record.usage.output_tokens,
        cache_read_tokens=record.usage.cache_read_tokens, cache_write_tokens=record.usage.cache_write_tokens,
        cost_usd=Decimal(str(record.cost_usd)).quantize(Decimal("0.000001")), cost_kes=usd_to_kes(record.cost_usd),
        duration_ms=record.duration_ms, verification_passed=record.verification_passed,
        problems=record.problems[:50], repairs=len(record.repairs), error=record.error or "",
    )
    if record.cost_usd:
        spend.record_spend(record.cost_usd)


def download_name(order):
    stem = re.sub(r"[^\w\- ]+", "", Path(order.original_name).stem).strip() or "deck"
    return f"{stem} (polished).pptx"


def unpaid_expiry():
    return timezone.now() + timedelta(hours=settings.UNPAID_RETENTION_HOURS)


def run_polish(order):
    """Generate the deck. Returns the PolishResult; on success the order is READY and unpaid."""
    planner, escalation = build_planners()
    with tempfile.TemporaryDirectory(prefix="subra-job-") as tmp:
        tmp = Path(tmp)
        src, out = fetch_original(order, tmp), tmp / "polished.pptx"
        result = polish(
            src, out, theme=order.theme.slug, planner=planner, escalation=escalation,
            before_attempt=lambda number, p: spend.ensure_capacity() if p.model else None,
            on_attempt=lambda record: log_attempt(order, record),
        )
        if not result.ok:
            return result
        replaced = store_result(order, out, make_previews(order, out, tmp))
        order.transition(
            S.READY, note=f"Verified, {len(result.attempts)} attempt(s), ${result.total_cost_usd:.4f}",
            plan=result.plan, overflow_slides=result.overflow_slides, failure_reason="", expires_at=unpaid_expiry(),
        )
        delete_files(replaced)
    return result



def run_rerender(order):
    """Re-render the saved plan in order.switching_to. Returns True if the switch took effect.

    A switch released as stuck (or overtaken by a newer one) may still finish; its output is discarded.
    """
    theme = order.switching_to
    with tempfile.TemporaryDirectory(prefix="subra-switch-") as tmp:
        tmp = Path(tmp)
        src, out = fetch_original(order, tmp), tmp / "polished.pptx"
        result = rerender(src, out, theme.slug, order.plan)
        if not result.ok:
            finish_switch_failed(order, theme, result.failure)
            return False
        previews = make_previews(order, out, tmp)
        with transaction.atomic():
            current = Order.objects.select_for_update().get(pk=order.pk)
            if current.status != S.RERENDERING or current.switching_to_id != theme.pk:
                log.warning("Theme switch for order %s finished after it was released; discarding it", order.pk)
                return False
            replaced = store_result(current, out, previews)
            current.transition(S.READY, note=f"Switched to {theme.slug}", theme=theme, switching_to=None,
                               switch_error="", overflow_slides=result.overflow_slides)
        delete_files(replaced)
    return True


def finish_switch_failed(order, theme, reason):
    """Back to READY with the previous deck untouched, unless a newer switch has taken over."""
    with transaction.atomic():
        current = Order.objects.select_for_update().get(pk=order.pk)
        if current.status != S.RERENDERING or current.switching_to_id != theme.pk:
            return
        current.transition(S.READY, note=f"Theme switch failed: {reason}"[:500], switching_to=None,
                           switch_error=f"We couldn't switch to {theme.name}; your deck is unchanged.")


def fetch_original(order, tmp):
    src = Path(tmp) / "original.pptx"
    with order.original_file.open("rb") as f, open(src, "wb") as dst:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            dst.write(chunk)
    return src


def make_previews(order, out, tmp):
    try:
        return render_previews(out, Path(tmp) / "previews", dpi=60)
    except PreviewError:
        log.exception("Preview rendering failed for order %s", order.pk)
        return []


def store_result(order, out, previews):
    """Put the polished file and its previews on the order; the caller's transition saves them.
    Returns the storage names they replace, to delete once the new ones are saved."""
    replaced = [n for n in [order.result_file.name, *order.preview_images] if n]
    with open(out, "rb") as f:
        order.result_file.save("polished.pptx", File(f), save=False)
    batch = uuid.uuid4().hex[:8]  # new names on every render, so a switch never overwrites the files it replaces
    names = []
    for i, png in enumerate(previews, start=1):
        with open(png, "rb") as f:
            names.append(default_storage.save(f"orders/{order.public_id}/preview-{batch}-{i:02d}.png", File(f)))
    order.preview_images = names
    return replaced


def delete_files(names):
    for name in names:
        if name:
            default_storage.delete(name)


def fail(order, reason):
    """Generation failed. Nothing was charged, so there's no refund; the admins hear about it anyway."""
    order.transition(S.FAILED, note=reason[:500], failure_reason=reason, expires_at=unpaid_expiry())
    alert_admins(
        f"Order {order.pk} failed to generate",
        f"Order {order.pk} ({order.original_name}, {order.slide_count} slides, {order.user.email}) could not be "
        f"polished after {order.ai_jobs.count()} attempt(s).\nReason: {reason}\n"
        f"AI cost: KES {order.ai_cost_kes:.2f}. The customer wasn't charged, so no refund is needed.",
    )


def delete_order_files(order):
    for name in [order.original_file.name, order.result_file.name, *order.preview_images]:
        if name:
            default_storage.delete(name)
    order.original_file = ""
    order.result_file = ""
    order.preview_images = []
    order.plan = None  # it quotes the start of some paragraphs, so it goes with the files
    order.files_deleted_at = timezone.now()
