from decimal import Decimal

from django.conf import settings
from django.contrib import admin
from django.db.models import DecimalField, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from billing.models import Payment

from . import spend
from .models import AIJob, DailySpend, Order, OrderEvent, Theme

ZERO = Value(Decimal("0"), output_field=DecimalField(max_digits=10, decimal_places=2))


class AIJobInline(admin.TabularInline):
    model = AIJob
    extra = 0
    can_delete = False
    fields = ("run", "attempt_number", "stage", "model", "input_tokens", "output_tokens", "cache_read_tokens",
              "cost_usd", "cost_kes", "duration_ms", "verification_passed", "repairs", "error")
    readonly_fields = fields


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    can_delete = False
    fields = ("created_at", "provider", "phone", "amount_kes", "fee_kes", "status", "provider_receipt", "result_code",
              "result_desc")
    readonly_fields = fields


class OrderEventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "from_status", "to_status", "note")
    readonly_fields = fields


class PaidFilter(admin.SimpleListFilter):
    title = "payment"
    parameter_name = "paid"

    def lookups(self, request, model_admin):
        return [("yes", "Paid"), ("no", "Unpaid")]

    def queryset(self, request, queryset):
        if self.value() == "yes":
            return queryset.filter(paid_at__isnull=False)
        if self.value() == "no":
            return queryset.filter(paid_at__isnull=True)
        return queryset


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("original_name", "user", "status", "paid", "slide_count", "price_kes", "ai_cost",
                    "payment_fees", "margin", "theme", "created_at")
    list_filter = ("status", PaidFilter, "product_type", "theme", "waiting_for_capacity")
    search_fields = ("original_name", "user__email", "public_id", "payments__provider_receipt", "payments__phone")
    readonly_fields = ("public_id", "user", "product_type", "original_name", "slide_count", "tier", "price_kes",
                       "status", "paid_at", "original_file", "result_file", "preview_images", "overflow_slides",
                       "qa_report",
                       "switch_error", "waiting_for_capacity", "failure_reason", "created_at", "updated_at",
                       "expires_at", "files_deleted_at", "ai_cost", "payment_fees", "margin")
    fields = readonly_fields + ("theme",)
    inlines = [PaymentInline, AIJobInline, OrderEventInline]
    date_hierarchy = "created_at"

    def get_queryset(self, request):
        paid_fees = Sum("payments__fee_kes", filter=Q(payments__status="paid"))
        return super().get_queryset(request).select_related("user", "theme").annotate(
            _ai_cost_kes=Coalesce(Subquery(
                AIJob.objects.filter(order=OuterRef("pk")).values("order").annotate(t=Sum("cost_kes")).values("t")
            ), ZERO),
            _fees_kes=Coalesce(paid_fees, ZERO),
        )

    @admin.display(description="AI cost (KES)", ordering="_ai_cost_kes")
    def ai_cost(self, obj):
        return f"{getattr(obj, '_ai_cost_kes', obj.ai_cost_kes):.2f}"

    @admin.display(description="Payment fees (KES)", ordering="_fees_kes")
    def payment_fees(self, obj):
        return f"{getattr(obj, '_fees_kes', obj.payment_fees_kes):.2f}"

    @admin.display(description="Paid", boolean=True, ordering="paid_at")
    def paid(self, obj):
        return obj.paid_at is not None

    @admin.display(description="Margin (KES)")
    def margin(self, obj):
        if obj.paid_at is None:
            return "-"
        cost = getattr(obj, "_ai_cost_kes", obj.ai_cost_kes)
        fees = getattr(obj, "_fees_kes", obj.payment_fees_kes)
        return f"{Decimal(obj.price_kes) - cost - fees:.2f}"


@admin.register(AIJob)
class AIJobAdmin(admin.ModelAdmin):
    list_display = ("order", "run", "attempt_number", "stage", "model", "input_tokens", "output_tokens", "cost_usd",
                    "cost_kes", "verification_passed", "created_at")
    list_filter = ("stage", "model", "verification_passed", "planner")
    readonly_fields = [f.name for f in AIJob._meta.fields]


@admin.register(DailySpend)
class DailySpendAdmin(admin.ModelAdmin):
    list_display = ("date", "total_cost_usd", "cap", "alert_sent")
    readonly_fields = ("date", "total_cost_usd", "alert_sent")

    @admin.display(description="Cap (USD)")
    def cap(self, obj):
        return settings.AI_DAILY_CAP_USD

    def changelist_view(self, request, extra_context=None):
        extra_context = {**(extra_context or {}), "month": spend.month_summary()}
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(Theme)
class ThemeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "active", "sort_order")
    list_editable = ("active", "sort_order")
