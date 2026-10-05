from django.contrib import admin, messages
from django.utils import timezone

from .models import Payment, PriceTier


@admin.register(PriceTier)
class PriceTierAdmin(admin.ModelAdmin):
    list_display = ("name", "product_type", "min_units", "max_units", "price_kes", "active")
    list_editable = ("price_kes", "active")
    list_filter = ("product_type", "active")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "order", "provider", "phone", "amount_kes", "fee_kes", "status",
                    "provider_receipt", "result_code", "callback_count")
    list_filter = ("provider", "status", "result_code")
    search_fields = ("phone", "provider_receipt", "provider_ref", "reference", "order__user__email")
    readonly_fields = [f.name for f in Payment._meta.fields]
    actions = ["mark_refunded"]

    @admin.action(description="Mark refunded (after refunding via M-Pesa)")
    def mark_refunded(self, request, queryset):
        now = timezone.now()
        done = queryset.filter(status=Payment.Status.PAID).update(status=Payment.Status.REFUNDED, refunded_at=now,
                                                                  updated_at=now)
        self.message_user(request, f"{done} payment(s) marked refunded.",
                          messages.SUCCESS if done else messages.WARNING)
