"""Generic billing: price tiers per product, payments, and (subscriptions spec) plans, credits and subscriptions."""

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q


class ProductType(models.TextChoices):
    DECK_POLISH = "deck_polish", "Deck polish"
    DOC_TO_DECK = "doc_to_deck", "Word document to deck"


class PriceTier(models.Model):
    """Editable in admin. Units are slides for deck_polish (pages for doc_to_deck, later)."""

    product_type = models.CharField(max_length=20, choices=ProductType.choices, default=ProductType.DECK_POLISH)
    name = models.CharField(max_length=50)
    min_units = models.PositiveIntegerField("min slides/pages")
    max_units = models.PositiveIntegerField("max slides/pages")
    price_kes = models.PositiveIntegerField("price (KES)")
    credits = models.PositiveIntegerField(default=0, help_text="Credits a subscriber pays instead; 0 = not offered")
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["product_type", "min_units"]

    def __str__(self):
        return f"{self.name} ({self.min_units}-{self.max_units}): KES {self.price_kes}"

    @classmethod
    def for_units(cls, product_type, units):
        return (
            cls.objects.filter(product_type=product_type, active=True, min_units__lte=units, max_units__gte=units)
            .order_by("price_kes")
            .first()
        )


class Payment(models.Model):
    """One payment attempt through a provider (Paystack at launch). Provider-neutral fields."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    class Kind(models.TextChoices):
        DECK = "deck", "Deck"
        SUBSCRIPTION = "subscription", "Subscription (first payment)"
        RENEWAL = "renewal", "Renewal"
        TOPUP = "topup", "Top-up"

    class Channel(models.TextChoices):
        MPESA = "mpesa", "M-Pesa"
        CARD = "card", "Card"

    kind = models.CharField(max_length=12, choices=Kind.choices, default=Kind.DECK)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.MPESA)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments", null=True,
                              blank=True)
    company = models.ForeignKey("accounts.Company", on_delete=models.PROTECT, related_name="payments", null=True,
                                blank=True)
    subscription = models.ForeignKey("billing.Subscription", on_delete=models.PROTECT, related_name="payments",
                                     null=True, blank=True)
    credit_pack = models.ForeignKey("billing.CreditPack", on_delete=models.PROTECT, null=True, blank=True)
    provider = models.CharField(max_length=20, default="paystack")
    provider_ref = models.CharField(max_length=64, null=True, blank=True, help_text="The provider's payment ID")
    reference = models.CharField(max_length=64, blank=True, help_text="Our reference sent to the provider")
    provider_receipt = models.CharField(max_length=64, blank=True, help_text="M-Pesa receipt number")
    phone = models.CharField(max_length=12, blank=True)
    amount_kes = models.PositiveIntegerField()
    fee_kes = models.DecimalField("fee (KES)", max_digits=10, decimal_places=2, default=0)
    fee_estimated = models.BooleanField(default=False, help_text="The provider didn't report its fee; estimated")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    refunded_at = models.DateTimeField(null=True, blank=True)
    result_code = models.CharField(max_length=32, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    raw_init = models.JSONField(null=True, blank=True)
    raw_callbacks = models.JSONField(default=list, blank=True)
    callback_count = models.PositiveIntegerField(default=0)
    status_checks = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "provider_ref"], name="unique_provider_ref"),
            models.CheckConstraint(condition=~Q(kind="deck") | Q(order__isnull=False), name="deck_payment_has_order"),
            models.CheckConstraint(condition=Q(kind="deck") | Q(company__isnull=False),
                                   name="card_payment_has_company"),
        ]

    def __str__(self):
        who = self.phone or (self.company.name if self.company_id else "")
        return f"KES {self.amount_kes} from {who} ({self.status})"

    FAILURE_MESSAGES = {
        "1032": "You cancelled the M-Pesa prompt.",
        "1037": "The M-Pesa prompt timed out before a PIN was entered.",
        "1": "Your M-Pesa balance was too low for this payment.",
        "2001": "The M-Pesa PIN was incorrect.",
        "timeout": "We didn't get a confirmation from M-Pesa.",
    }
    CARD_FAILURE_MESSAGES = {
        "abandoned": "The card payment wasn't finished.",
        "timeout": "We didn't get a confirmation from Paystack.",
    }

    @property
    def customer_message(self):
        if self.channel == self.Channel.CARD:
            return self.CARD_FAILURE_MESSAGES.get(self.result_code, "The card payment didn't go through.")
        return self.FAILURE_MESSAGES.get(self.result_code, "M-Pesa didn't complete the payment.")


# --- subscriptions spec: plans, packs, subscriptions, the credits ledger ----------------------------------------


class Plan(models.Model):
    """A monthly card subscription. Each one is mirrored as a Paystack Plan (manage.py sync_paystack_plans)."""

    code = models.SlugField(unique=True)
    name = models.CharField(max_length=50)
    price_kes = models.PositiveIntegerField("price a month (KES)")
    monthly_credits = models.PositiveIntegerField()
    paystack_plan_code = models.CharField(max_length=40, blank=True, help_text="PLN_..., set by sync_paystack_plans")
    paystack_amount_kes = models.PositiveIntegerField(default=0, help_text="The amount Paystack last confirmed")
    paystack_mode = models.CharField(max_length=4, blank=True, help_text="test or live: the key the plan was made with")
    active = models.BooleanField(default=True)
    staff_only = models.BooleanField(default=False, help_text="Only staff see and can buy it, e.g. a cheap plan for "
                                                              "testing a real card")
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price_kes"]

    def __str__(self):
        return f"{self.name}: KES {self.price_kes:,} a month, {self.monthly_credits} credits"

    @classmethod
    def offered_to(cls, user):
        plans = cls.objects.filter(active=True)
        return plans if getattr(user, "is_staff", False) else plans.filter(staff_only=False)

    @property
    def kes_per_credit(self):
        return round(self.price_kes / self.monthly_credits, 1) if self.monthly_credits else 0

    @property
    def carryover_cap(self):
        return self.monthly_credits * settings.CREDIT_CARRYOVER_MULTIPLE


class CreditPack(models.Model):
    """A one-off top-up for a subscribed company, paid by card or M-Pesa on Paystack's page."""

    name = models.CharField(max_length=50)
    credits = models.PositiveIntegerField()
    price_kes = models.PositiveIntegerField("price (KES)")
    active = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "price_kes"]

    def __str__(self):
        return f"{self.name}: {self.credits} credits for KES {self.price_kes:,}"


class InvalidSubscriptionTransition(Exception):
    pass


class Subscription(models.Model):
    """SUBRA's record of a Paystack subscription. Paystack charges the card; this tracks what the company may do."""

    class Status(models.TextChoices):
        INCOMPLETE = "incomplete", "Checkout open"
        ACTIVE = "active", "Active"
        PAST_DUE = "past_due", "Card payment failed"
        CANCELLING = "cancelling", "Cancelled, ends at period end"
        CANCELLED = "cancelled", "Ended"
        LAPSED = "lapsed", "Lapsed (card not fixed)"
        ABANDONED = "abandoned", "Checkout abandoned"

    S = Status
    LIVE = (S.ACTIVE, S.PAST_DUE, S.CANCELLING)  # credits can be spent
    TERMINAL = (S.CANCELLED, S.LAPSED, S.ABANDONED)
    TRANSITIONS = {
        S.INCOMPLETE: {S.ACTIVE, S.ABANDONED},
        S.ACTIVE: {S.ACTIVE, S.PAST_DUE, S.CANCELLING, S.CANCELLED},
        S.PAST_DUE: {S.ACTIVE, S.CANCELLING, S.LAPSED, S.CANCELLED},
        S.CANCELLING: {S.ACTIVE, S.CANCELLED},
        S.CANCELLED: set(),
        S.LAPSED: set(),
        S.ABANDONED: set(),
    }

    company = models.ForeignKey("accounts.Company", on_delete=models.PROTECT, related_name="subscriptions")
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INCOMPLETE)
    paystack_subscription_code = models.CharField(max_length=40, blank=True, db_index=True)
    paystack_email_token = models.CharField(max_length=40, blank=True)
    paystack_customer_code = models.CharField(max_length=40, blank=True, db_index=True)
    paystack_customer_id = models.CharField(max_length=20, blank=True)
    email = models.EmailField(help_text="The email Paystack knows the customer by")
    card_last4 = models.CharField(max_length=4, blank=True)
    card_brand = models.CharField(max_length=20, blank=True)
    card_exp = models.CharField(max_length=7, blank=True, help_text="MM/YYYY")
    renews_at = models.DateTimeField(null=True, blank=True, help_text="Paystack's next payment date")
    periods_paid = models.PositiveIntegerField(default=0)
    past_due_since = models.DateTimeField(null=True, blank=True)
    grace_ends_at = models.DateTimeField(null=True, blank=True)
    reminder_sent_for = models.DateTimeField(null=True, blank=True, help_text="The renews_at last reminded about")
    grace_warning_sent = models.BooleanField(default=False)
    consent_text = models.TextField(blank=True)
    consented_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    replaces = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                 help_text="The subscription this one takes over from (plan change or new card)")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True, help_text="Paystack's subscription, as last read")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["company"], condition=Q(status__in=["active", "past_due", "cancelling"]),
                                    name="one_live_subscription_per_company"),
            models.UniqueConstraint(fields=["company"], condition=Q(status="incomplete"),
                                    name="one_open_checkout_per_company"),
        ]

    def __str__(self):
        return f"{self.company} on {self.plan.name} ({self.get_status_display().lower()})"

    @property
    def is_live(self):
        return self.status in self.LIVE

    @property
    def credits_usable_until(self):
        if self.status == self.S.PAST_DUE:
            return self.grace_ends_at
        if self.status == self.S.CANCELLING:
            return self.renews_at
        return None

    def can_transition(self, to):
        return to in self.TRANSITIONS.get(self.status, set())

    def transition(self, to, note="", **fields):
        if not self.can_transition(to):
            raise InvalidSubscriptionTransition(f"{self.status} -> {to} is not allowed")
        from_status = self.status
        with transaction.atomic():
            self.status = to
            for k, v in fields.items():
                setattr(self, k, v)
            self.save()
            SubscriptionEvent.objects.create(subscription=self, from_status=from_status, to_status=to,
                                             note=note[:500])

    def log(self, note):
        SubscriptionEvent.objects.create(subscription=self, from_status=self.status, to_status=self.status,
                                         note=note[:500])


class SubscriptionEvent(models.Model):
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="events")
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12)
    note = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class CreditTransaction(models.Model):
    """Append-only credits ledger, one stream per company. Written only through billing.credits."""

    class Kind(models.TextChoices):
        GRANT = "grant", "Monthly credits"
        TOPUP = "topup", "Top-up"
        DEBIT = "debit", "Deck download"
        REFUND = "refund", "Refund"
        EXPIRE = "expire", "Expired"
        CLAWBACK = "clawback", "Payment reversed"
        ADJUST = "adjust", "Adjustment"

    company = models.ForeignKey("accounts.Company", on_delete=models.PROTECT, related_name="credit_transactions")
    delta = models.IntegerField()
    balance_after = models.IntegerField()
    kind = models.CharField(max_length=10, choices=Kind.choices)
    idempotency_key = models.CharField(max_length=100, unique=True)
    kes_value = models.DecimalField("value (KES)", max_digits=10, decimal_places=2, default=0)
    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, null=True, blank=True,
                              related_name="credit_transactions")
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="credit_transactions")
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT, null=True, blank=True,
                                     related_name="credit_transactions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="+", help_text="Who spent the credits")
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", help_text="The staff member, for adjustments")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["company", "-id"])]

    def __str__(self):
        return f"{self.delta:+d} ({self.get_kind_display()}) for {self.company}, balance {self.balance_after}"


class WebhookDelivery(models.Model):
    """Every webhook body, stored before it's processed, so ignored and failed events leave a trace."""

    provider = models.CharField(max_length=20)
    event_type = models.CharField(max_length=60, blank=True)
    reference = models.CharField(max_length=100, blank=True, db_index=True)
    body = models.JSONField(default=dict)
    outcome = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "webhook deliveries"

    def __str__(self):
        return f"{self.provider} {self.event_type} {self.reference}"
