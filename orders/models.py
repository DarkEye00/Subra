import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.templatetags.static import static
from django.utils import timezone

from billing.models import PriceTier, ProductType


class Theme(models.Model):
    """A built-in design. ``slug`` must match a theme in polisher/render/themes.py."""

    name = models.CharField(max_length=60)
    slug = models.SlugField(unique=True)
    description = models.CharField(max_length=200, blank=True)
    template_file = models.FileField(upload_to="themes/", blank=True, help_text="Reserved for customer brand kits")
    preview_image = models.ImageField(upload_to="themes/", blank=True)
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name

    @property
    def preview_url(self):
        return self.preview_image.url if self.preview_image else static(f"themes/{self.slug}.png")


def _order_path(order, filename, kind):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"orders/{order.public_id}/{kind}.{ext}"


def original_upload_path(order, filename):
    return _order_path(order, filename, "original")


def result_upload_path(order, filename):
    return _order_path(order, filename, f"polished-{uuid.uuid4().hex[:8]}")


class InvalidTransition(Exception):
    pass


class Order(models.Model):
    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Uploaded"
        QUOTED = "quoted", "Quoted"
        PAYMENT_PENDING = "payment_pending", "Waiting for M-Pesa"
        PAYMENT_FAILED = "payment_failed", "Payment failed"
        PAID = "paid", "Paid, in queue"
        PROCESSING = "processing", "Polishing"
        DONE = "done", "Ready"
        FAILED = "failed", "Failed"
        REFUND_PENDING = "refund_pending", "Refund pending"
        REFUNDED = "refunded", "Refunded"
        EXPIRED = "expired", "Expired (files deleted)"

    S = Status
    TRANSITIONS = {
        S.UPLOADED: {S.QUOTED, S.EXPIRED},
        S.QUOTED: {S.PAYMENT_PENDING, S.EXPIRED},
        S.PAYMENT_PENDING: {S.PAID, S.PAYMENT_FAILED},
        S.PAYMENT_FAILED: {S.PAYMENT_PENDING, S.EXPIRED},
        S.PAID: {S.PROCESSING},
        S.PROCESSING: {S.DONE, S.FAILED, S.PAID},  # back to PAID = re-queued (spend cap or AI outage)
        S.DONE: {S.PAID, S.EXPIRED},  # back to PAID = the free re-run, queued
        S.FAILED: {S.REFUND_PENDING},
        S.REFUND_PENDING: {S.REFUNDED},
        S.REFUNDED: {S.EXPIRED},
        S.EXPIRED: set(),
    }

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    product_type = models.CharField(max_length=20, choices=ProductType.choices, default=ProductType.DECK_POLISH)
    original_file = models.FileField(upload_to=original_upload_path, blank=True)
    original_name = models.CharField(max_length=255)
    slide_count = models.PositiveIntegerField(default=0)
    tier = models.ForeignKey(PriceTier, on_delete=models.PROTECT, null=True, blank=True)
    price_kes = models.PositiveIntegerField(default=0)
    theme = models.ForeignKey(Theme, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.UPLOADED)
    result_file = models.FileField(upload_to=result_upload_path, blank=True)
    preview_images = models.JSONField(default=list, blank=True)  # storage names, in slide order
    overflow_slides = models.JSONField(default=list, blank=True)
    rerun_used = models.BooleanField(default=False)
    waiting_for_capacity = models.BooleanField(default=False)
    failure_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    files_deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "expires_at"])]

    def __str__(self):
        return f"{self.original_name} ({self.get_status_display()})"

    def save(self, *args, **kwargs):
        if self.expires_at is None:
            self.expires_at = timezone.now() + timedelta(days=settings.FILE_RETENTION_DAYS)
        super().save(*args, **kwargs)

    def can_transition(self, to):
        return to in self.TRANSITIONS.get(self.status, set())

    def transition(self, to, note="", **fields):
        """Change status (and any extra fields) and log it. Raises InvalidTransition."""
        if not self.can_transition(to):
            raise InvalidTransition(f"{self.status} -> {to} is not allowed")
        from_status = self.status
        with transaction.atomic():
            self.status = to
            for k, v in fields.items():
                setattr(self, k, v)
            self.save()
            OrderEvent.objects.create(order=self, from_status=from_status, to_status=to, note=note[:500])

    # --- money --------------------------------------------------------------

    @property
    def ai_cost_usd(self):
        return sum((j.cost_usd for j in self.ai_jobs.all()), Decimal("0"))

    @property
    def ai_cost_kes(self):
        return sum((j.cost_kes for j in self.ai_jobs.all()), Decimal("0"))

    @property
    def margin_kes(self):
        """Price minus AI cost and payment fees (only meaningful for paid orders)."""
        fees = Decimal(self.price_kes) * Decimal(str(settings.PAYMENT_FEE_RATE))
        return Decimal(self.price_kes) - self.ai_cost_kes - fees

    @property
    def is_paid(self):
        return self.payments.filter(status="paid").exists()

    @property
    def can_rerun(self):
        return self.status == self.Status.DONE and not self.rerun_used and not self.files_deleted_at


class OrderEvent(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=20, blank=True)
    to_status = models.CharField(max_length=20)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class AIJob(models.Model):
    """One planner attempt: tokens, cost, and whether its deck passed verification."""

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="ai_jobs")
    run = models.PositiveIntegerField(default=1, help_text="1 = first run, 2 = free re-run")
    attempt_number = models.PositiveIntegerField()
    planner = models.CharField(max_length=20, default="claude")
    model = models.CharField(max_length=64, blank=True)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    cost_kes = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    verification_passed = models.BooleanField(null=True)
    problems = models.JSONField(default=list, blank=True)
    repairs = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class DailySpend(models.Model):
    """AI spend per day (Nairobi time), for the daily cap."""

    date = models.DateField(unique=True)
    total_cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    alert_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date}: ${self.total_cost_usd}"
