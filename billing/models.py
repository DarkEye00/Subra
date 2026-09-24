"""Generic billing: price tiers per product and payments against orders."""

from django.db import models


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
    """One payment attempt through a provider (IntaSend at launch). Provider-neutral fields."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=20, default="intasend")
    provider_ref = models.CharField(max_length=64, null=True, blank=True, help_text="The provider's payment ID")
    reference = models.CharField(max_length=64, blank=True, help_text="Our reference sent to the provider")
    provider_receipt = models.CharField(max_length=64, blank=True, help_text="M-Pesa receipt number")
    phone = models.CharField(max_length=12)
    amount_kes = models.PositiveIntegerField()
    fee_kes = models.DecimalField("fee (KES)", max_digits=10, decimal_places=2, default=0)
    fee_estimated = models.BooleanField(default=False, help_text="The provider didn't report its fee; estimated")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
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
        constraints = [models.UniqueConstraint(fields=["provider", "provider_ref"], name="unique_provider_ref")]

    def __str__(self):
        return f"KES {self.amount_kes} from {self.phone} ({self.status})"

    FAILURE_MESSAGES = {
        "1032": "You cancelled the M-Pesa prompt.",
        "1037": "The M-Pesa prompt timed out before a PIN was entered.",
        "1": "Your M-Pesa balance was too low for this payment.",
        "2001": "The M-Pesa PIN was incorrect.",
        "timeout": "We didn't get a confirmation from M-Pesa.",
    }

    @property
    def customer_message(self):
        return self.FAILURE_MESSAGES.get(self.result_code, "M-Pesa didn't complete the payment.")
