from datetime import timedelta

from django.contrib import admin, messages
from django.utils import timezone

from . import credits, subscriptions
from .models import (CreditPack, CreditTransaction, Payment, Plan, PriceTier, Subscription, SubscriptionEvent,
                     WebhookDelivery)
from .providers import ProviderError


@admin.register(PriceTier)
class PriceTierAdmin(admin.ModelAdmin):
    list_display = ("name", "product_type", "min_units", "max_units", "price_kes", "credits", "active")
    list_editable = ("price_kes", "credits", "active")
    list_filter = ("product_type", "active")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("created_at", "kind", "channel", "order", "company", "provider", "phone", "amount_kes", "fee_kes",
                    "status", "provider_receipt", "result_code", "callback_count")
    list_filter = ("kind", "channel", "provider", "status", "result_code")
    search_fields = ("phone", "provider_receipt", "provider_ref", "reference", "order__user__email",
                     "company__name", "company__owner__email")
    readonly_fields = [f.name for f in Payment._meta.fields]
    actions = ["mark_refunded"]

    @admin.action(description="Mark refunded (after refunding at the provider); claws back card credits")
    def mark_refunded(self, request, queryset):
        done = 0
        for payment in queryset.filter(status=Payment.Status.PAID):
            subscriptions.mark_refunded(payment)
            done += 1
        self.message_user(request, f"{done} payment(s) marked refunded.",
                          messages.SUCCESS if done else messages.WARNING)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "price_kes", "monthly_credits", "kes_per_credit", "paystack_plan_code",
                    "paystack_amount_kes", "active", "sort_order")
    list_editable = ("price_kes", "monthly_credits", "active", "sort_order")
    readonly_fields = ("paystack_plan_code", "paystack_amount_kes")

    def changelist_view(self, request, extra_context=None):
        stale = [p.name for p in Plan.objects.filter(active=True) if p.paystack_amount_kes != p.price_kes]
        if stale:
            self.message_user(request, f"Not yet at Paystack: {', '.join(stale)}. Run manage.py sync_paystack_plans "
                              "(new subscribers are refused until then).", messages.WARNING)
        return super().changelist_view(request, extra_context=extra_context)


@admin.register(CreditPack)
class CreditPackAdmin(admin.ModelAdmin):
    list_display = ("name", "credits", "price_kes", "active", "sort_order")
    list_editable = ("credits", "price_kes", "active", "sort_order")


class SubscriptionEventInline(admin.TabularInline):
    model = SubscriptionEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "from_status", "to_status", "note")
    readonly_fields = fields


class SubscriptionPaymentInline(admin.TabularInline):
    model = Payment
    fk_name = "subscription"
    extra = 0
    can_delete = False
    fields = ("created_at", "kind", "reference", "amount_kes", "fee_kes", "status", "result_desc")
    readonly_fields = fields


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("company", "plan", "status", "renews_at", "grace_ends_at", "card_last4", "periods_paid",
                    "paystack_subscription_code", "created_at")
    list_filter = ("status", "plan")
    search_fields = ("company__name", "email", "paystack_subscription_code", "paystack_customer_code")
    exclude = ("paystack_email_token",)
    inlines = [SubscriptionPaymentInline, SubscriptionEventInline]
    actions = ["resync", "extend_grace"]

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in Subscription._meta.fields if f.name != "paystack_email_token"]

    def has_add_permission(self, request):
        return False

    @admin.action(description="Re-read from Paystack")
    def resync(self, request, queryset):
        for sub in queryset:
            try:
                subscriptions.sync_subscription(sub)
            except ProviderError as e:
                self.message_user(request, f"{sub}: {e}", messages.ERROR)
        self.message_user(request, "Re-read from Paystack.")

    @admin.action(description="Extend grace by 7 days (past-due plans)")
    def extend_grace(self, request, queryset):
        done = 0
        for sub in queryset.filter(status=Subscription.Status.PAST_DUE):
            sub.grace_ends_at = max(sub.grace_ends_at or timezone.now(), timezone.now()) + timedelta(days=7)
            sub.grace_warning_sent = False
            sub.save(update_fields=["grace_ends_at", "grace_warning_sent", "updated_at"])
            sub.log(f"Grace extended to {sub.grace_ends_at:%d %b %Y} by {request.user}")
            done += 1
        self.message_user(request, f"Extended {done} subscription(s).")


@admin.register(CreditTransaction)
class CreditTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "company", "kind", "delta", "balance_after", "kes_value", "user", "order", "note")
    list_filter = ("kind",)
    search_fields = ("company__name", "user__email", "note", "idempotency_key")
    readonly_fields = [f.name for f in CreditTransaction._meta.fields]
    actions = ["refund_deck_credits"]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Give back the credits for these deck downloads")
    def refund_deck_credits(self, request, queryset):
        done = sum(1 for row in queryset.filter(kind=CreditTransaction.Kind.DEBIT)
                   if credits.refund_deck(row.order, request.user) is not None)
        self.message_user(request, f"Refunded {done} deck(s).")


@admin.register(WebhookDelivery)
class WebhookDeliveryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "provider", "event_type", "reference", "outcome")
    list_filter = ("provider", "event_type")
    search_fields = ("reference", "outcome")
    readonly_fields = [f.name for f in WebhookDelivery._meta.fields]

    def has_add_permission(self, request):
        return False
