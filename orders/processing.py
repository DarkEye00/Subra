"""Run the polisher for an order: fetch files, call the pipeline, store results, log AI cost."""

import logging
import re
import tempfile
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage
from django.utils import timezone

from core.alerts import alert_admins
from polisher.pipeline import polish
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


def log_attempt(order, run, record):
    AIJob.objects.create(
        order=order, run=run, attempt_number=record.number, planner=record.planner, model=record.model or "",
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


def run_polish(order, run):
    """Returns the PolishResult; stores the result and previews on success."""
    planner, escalation = build_planners()
    with tempfile.TemporaryDirectory(prefix="subra-job-") as tmp:
        tmp = Path(tmp)
        src, out = tmp / "original.pptx", tmp / "polished.pptx"
        with order.original_file.open("rb") as f, open(src, "wb") as dst:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                dst.write(chunk)
        result = polish(
            src, out, theme=order.theme.slug, planner=planner, escalation=escalation,
            before_attempt=lambda number, p: spend.ensure_capacity() if p.model else None,
            on_attempt=lambda record: log_attempt(order, run, record),
        )
        if not result.ok:
            return result
        try:
            previews = render_previews(out, tmp / "previews", dpi=60)
        except PreviewError:
            log.exception("Preview rendering failed for order %s", order.pk)
            previews = []
        old_files = [order.result_file.name, *order.preview_images] if order.result_file else []
        with open(out, "rb") as f:
            order.result_file.save("polished.pptx", File(f), save=False)
        names = []
        for i, png in enumerate(previews, start=1):
            with open(png, "rb") as f:
                names.append(default_storage.save(f"orders/{order.public_id}/preview-r{run}-{i:02d}.png", File(f)))
        order.transition(
            S.DONE, note=f"run {run}: verified, {len(result.attempts)} attempt(s), ${result.total_cost_usd:.4f}",
            preview_images=names, overflow_slides=result.overflow_slides, failure_reason="",
            expires_at=timezone.now() + timedelta(days=settings.FILE_RETENTION_DAYS),
        )
        for name in old_files:
            if name:
                default_storage.delete(name)
    return result


def fail(order, run, reason):
    if run == 2:
        # a failed free re-run leaves the first result in place, and they may try again
        order.transition(S.DONE, note=f"Re-run failed: {reason}"[:500], rerun_used=False)
        return
    order.transition(S.FAILED, note=reason[:500], failure_reason=reason)
    order.transition(S.REFUND_PENDING, note="Automatic: processing failed")
    alert_admins(
        f"Order {order.pk} failed: refund needed",
        f"Order {order.pk} ({order.original_name}, KES {order.price_kes}, {order.user.email}) could not be "
        f"polished.\nReason: {reason}\nRefund it via M-Pesa and mark it refunded in the admin.",
    )


def delete_order_files(order):
    for name in [order.original_file.name, order.result_file.name, *order.preview_images]:
        if name:
            default_storage.delete(name)
    order.original_file = ""
    order.result_file = ""
    order.preview_images = []
    order.files_deleted_at = timezone.now()
