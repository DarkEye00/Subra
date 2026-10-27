import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction
from django.templatetags.static import static
from django.utils import timezone

from billing.models import Payment, PriceTier, ProductType


class Theme(models.Model):
    """A built-in design. ``slug`` must match a theme in polisher/render/themes.py, where its
    colour scheme, font pair, and background specs live (Amendment 1B: no template file)."""

    name = models.CharField(max_length=60)
    slug = models.SlugField(unique=True)
    description = models.CharField(max_length=200, blank=True)
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
    """Processing status and payment are separate tracks (Amendment 2): customers see the deck, then pay."""

    class Status(models.TextChoices):
        QUOTED = "quoted", "Ready to polish"
        QUEUED = "queued", "In queue"
        PROCESSING = "processing", "Polishing"
        READY = "ready", "Ready"
        RERENDERING = "rerendering", "Switching theme"
        FAILED = "failed", "Couldn't polish"
        EXPIRED = "expired", "Expired (files deleted)"

    S = Status
    TRANSITIONS = {
        S.QUOTED: {S.QUEUED, S.EXPIRED},
        S.QUEUED: {S.PROCESSING},
        S.PROCESSING: {S.READY, S.FAILED, S.QUEUED},  # back to QUEUED = spend cap or AI outage
        S.READY: {S.RERENDERING, S.EXPIRED},
        S.RERENDERING: {S.READY},
        S.FAILED: {S.EXPIRED},
        S.EXPIRED: set(),
    }
    UNPAID_WORK = (S.QUEUED, S.PROCESSING, S.READY, S.RERENDERING)  # counts toward one unpaid deck per account
    IN_FLIGHT = (S.QUEUED, S.PROCESSING, S.RERENDERING)

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="orders")
    product_type = models.CharField(max_length=20, choices=ProductType.choices, default=ProductType.DECK_POLISH)
    original_file = models.FileField(upload_to=original_upload_path, blank=True)
    original_name = models.CharField(max_length=255)
    slide_count = models.PositiveIntegerField(default=0)
    tier = models.ForeignKey(PriceTier, on_delete=models.PROTECT, null=True, blank=True)
    price_kes = models.PositiveIntegerField(default=0)
    credit_cost = models.PositiveIntegerField(default=0, help_text="Credits a subscriber pays instead (0 = not offered)")
    company = models.ForeignKey("accounts.Company", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="orders", help_text="The uploader's company at upload")
    theme = models.ForeignKey(Theme, on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUOTED)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_with = models.CharField(max_length=10, blank=True, choices=[("mpesa", "M-Pesa"), ("credits", "Credits")])
    plan = models.JSONField(null=True, blank=True)  # the verified layout plan; theme switches re-render it
    result_file = models.FileField(upload_to=result_upload_path, blank=True)
    preview_images = models.JSONField(default=list, blank=True)  # storage names, in slide order
    overflow_slides = models.JSONField(default=list, blank=True)  # rendered slides with a remaining layout fault
    qa_report = models.JSONField(default=dict, blank=True)  # retention, repairs, fallbacks, cost and time per stage
    locked_summary = models.JSONField(default=dict, blank=True)  # locked objects found at upload (Amendment 1C)
    switch_error = models.CharField(max_length=200, blank=True)
    switching_to = models.ForeignKey(Theme, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
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
            self.expires_at = timezone.now() + timedelta(hours=settings.UNPAID_RETENTION_HOURS)
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

    def log(self, note):
        """Record an event that doesn't change the status, such as a payment."""
        OrderEvent.objects.create(order=self, from_status=self.status, to_status=self.status, note=note[:500])

    # --- payment ------------------------------------------------------------

    @property
    def is_paid(self):
        return self.paid_at is not None

    @property
    def credits_paid_note(self):
        """e.g. "Paid with 7 credits by wanjiku@example.co.ke" for a credit-paid deck."""
        if self.paid_with != "credits":
            return ""
        debit = self.credit_transactions.filter(kind="debit").select_related("user").first()
        if debit is None:
            return "Paid with credits"
        who = f" by {debit.user.email}" if debit.user_id else ""
        return f"Paid with {-debit.delta} credits{who}"

    @property
    def has_pending_payment(self):
        return self.payments.filter(status=Payment.Status.PENDING).exists()

    @property
    def can_pay(self):
        return (self.status == self.Status.READY and not self.is_paid and not self.files_deleted_at
                and not self.has_pending_payment)

    @property
    def can_switch_theme(self):
        return (self.status == self.Status.READY and bool(self.plan) and not self.files_deleted_at
                and not self.has_pending_payment)

    @property
    def can_discard(self):
        if self.is_paid or self.files_deleted_at:
            return False
        if self.status in (self.Status.QUOTED, self.Status.FAILED):
            return True
        return self.status == self.Status.READY and not self.has_pending_payment

    # --- money --------------------------------------------------------------

    @property
    def ai_cost_usd(self):
        return sum((j.cost_usd for j in self.ai_jobs.all()), Decimal("0"))

    @property
    def ai_cost_kes(self):
        return sum((j.cost_kes for j in self.ai_jobs.all()), Decimal("0"))

    @property
    def payment_fees_kes(self):
        return sum((p.fee_kes for p in self.payments.filter(status="paid")), Decimal("0"))

    @property
    def revenue_kes(self):
        """What the deck earned: its KES price, or for a credit-paid deck what those credits were worth."""
        if self.paid_with == "credits":
            debit = self.credit_transactions.filter(kind="debit").first()
            return debit.kes_value if debit else Decimal("0")
        return Decimal(self.price_kes)

    @property
    def margin_kes(self):
        """Revenue minus AI cost and payment provider fees (only meaningful for paid orders). A credit-paid deck
        carries no fee of its own: the card fee sits on the subscription payment."""
        return self.revenue_kes - self.ai_cost_kes - self.payment_fees_kes

    LOCKED_WORDS = {
        "video": "video", "audio": "audio", "ole": "embedded objects", "equation": "equations",
        "model3d": "3D models", "smartart": "SmartArt diagrams", "ink": "ink drawings",
        "group": "grouped media", "linked_image": "linked pictures", "other": "special objects",
    }

    @property
    def locked_description(self):
        """e.g. "4 slides contain video and embedded objects." (empty if there are none)"""
        counts = (self.locked_summary or {}).get("counts") or {}
        if not counts:
            return ""
        words = list(dict.fromkeys(self.LOCKED_WORDS.get(k, "special objects") for k in counts))
        what = words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]
        n = self.locked_summary.get("slides", 0)
        return f"{n} slide{'s' if n != 1 else ''} contain{'' if n != 1 else 's'} {what}."

    @property
    def qa_notes(self):
        """What the preview page tells the customer about repaired and flagged slides (17 Oct build plan)."""
        report = self.qa_report or {}
        notes = []
        repaired = report.get("repaired_slides") or []
        if repaired:
            notes.append(f"We re-laid out {_slides_phrase(repaired)} so everything fits.")
        trouble = sorted(set(report.get("fallback_slides") or []) | set(report.get("flagged_slides") or []))
        if trouble:
            phrase = _slides_phrase(trouble)
            phrase = phrase[0].upper() + phrase[1:]
            if len(trouble) == 1:
                notes.append(f"{phrase} has more content than fits comfortably on one slide, so we used a simpler "
                             "layout. Check it in PowerPoint.")
            else:
                notes.append(f"{phrase} have more content than fits comfortably on one slide, so we used simpler "
                             "layouts. Check them in PowerPoint.")
        return notes

    @property
    def has_linked_objects(self):
        return bool((self.locked_summary or {}).get("linked"))

    @property
    def preview_caveats(self):
        """LibreOffice previews can't show everything exactly (Amendment 1C)."""
        summary = self.locked_summary or {}
        out = []
        if summary.get("equations"):
            out.append("equations may look slightly different")
        if summary.get("model3d"):
            out.append("3D models appear as still images")
        return out


def _slides_phrase(indexes):
    nums = [str(i) for i in indexes]
    if len(nums) == 1:
        return f"slide {nums[0]}"
    return "slides " + ", ".join(nums[:-1]) + " and " + nums[-1]


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
    run = models.PositiveIntegerField(default=1, help_text="1 = the generation; theme switches make no AI calls")
    attempt_number = models.PositiveIntegerField()
    stage = models.CharField(max_length=12, default="plan")  # classify | plan | repair | vision
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
