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
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"

    order = models.ForeignKey("orders.Order", on_delete=models.PROTECT, related_name="payments")
    phone = models.CharField(max_length=12)
    amount_kes = models.PositiveIntegerField()
    merchant_request_id = models.CharField(max_length=64, blank=True)
    checkout_request_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    mpesa_receipt = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    result_code = models.CharField(max_length=16, blank=True)
    result_desc = models.CharField(max_length=255, blank=True)
    raw_callback = models.JSONField(null=True, blank=True)
    callback_count = models.PositiveIntegerField(default=0)
    status_checks = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"KES {self.amount_kes} from {self.phone} ({self.status})"
