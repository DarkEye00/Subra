from django.contrib import admin

from .models import Payment, PriceTier


@admin.register(PriceTier)
class PriceTierAdmin(admin.ModelAdmin):
    list_display = ("name", "product_type", "min_units", "max_units", "price_kes", "active")
    list_editable = ("price_kes", "active")
    list_filter = ("product_type", "active")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "order", "phone", "amount_kes", "status", "mpesa_receipt", "result_code",
                    "callback_count")
    list_filter = ("status", "result_code")
    search_fields = ("phone", "mpesa_receipt", "checkout_request_id", "order__user__email")
    readonly_fields = [f.name for f in Payment._meta.fields]
