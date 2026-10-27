from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Company, Invite, Membership, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ("-date_joined",)
    list_display = ("email", "mpesa_phone", "is_staff", "date_joined")
    search_fields = ("email", "mpesa_phone")
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Profile", {"fields": ("first_name", "last_name", "mpesa_phone")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("email", "password1", "password2")}),)


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ("user", "role", "invited_by", "created_at")
    readonly_fields = ("user", "role", "invited_by", "created_at")
    can_delete = True


class InviteInline(admin.TabularInline):
    model = Invite
    extra = 0
    fields = ("email", "invited_by", "created_at", "expires_at", "accepted_at", "revoked_at")
    readonly_fields = fields
    can_delete = False


class CompanyForm(forms.ModelForm):
    class Meta:
        model = Company
        fields = ("name", "owner", "archived_at")

    def clean_owner(self):
        owner = self.cleaned_data["owner"]
        if self.instance.pk and owner != self.instance.owner and not self.instance.memberships.filter(user=owner).exists():
            raise forms.ValidationError("The new owner must already be a member of this company.")
        return owner


class AdjustCreditsForm(forms.Form):
    delta = forms.IntegerField(label="Credits (+ to add, - to remove)")
    note = forms.CharField(max_length=200, help_text="Why, for the ledger (the customer may see it)")


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    form = CompanyForm
    list_display = ("name", "owner", "balance", "plan", "members", "created_at", "archived_at")
    search_fields = ("name", "owner__email", "memberships__user__email")
    inlines = [MembershipInline, InviteInline]
    readonly_fields = ("balance", "plan", "adjust_link")

    @admin.display(description="Credits")
    def balance(self, obj):
        from billing.credits import balance

        return balance(obj)

    @admin.display(description="Plan")
    def plan(self, obj):
        from billing.credits import live_subscription

        sub = live_subscription(obj)
        return f"{sub.plan.name} ({sub.get_status_display().lower()})" if sub else "-"

    @admin.display(description="Members")
    def members(self, obj):
        return obj.memberships.count()

    @admin.display(description="Adjust credits")
    def adjust_link(self, obj):
        if not obj.pk:
            return "-"
        return format_html('<a href="{}">Add or remove credits</a>', reverse("admin:accounts_company_adjust", args=[obj.pk]))

    def save_model(self, request, obj, form, change):
        """Changing the owner here is the ownership hand-over (staff-only in v1): the roles follow."""
        super().save_model(request, obj, form, change)
        if change and "owner" in form.changed_data:
            obj.memberships.filter(role=Membership.Role.OWNER).exclude(user=obj.owner).update(role=Membership.Role.MEMBER)
            obj.memberships.filter(user=obj.owner).update(role=Membership.Role.OWNER)

    def get_urls(self):
        return [path("<int:pk>/adjust/", self.admin_site.admin_view(self.adjust_view),
                     name="accounts_company_adjust")] + super().get_urls()

    def adjust_view(self, request, pk):
        from billing import credits

        company = get_object_or_404(Company, pk=pk)
        form = AdjustCreditsForm(request.POST or None)
        if request.method == "POST" and form.is_valid():
            row = credits.adjust(company, form.cleaned_data["delta"], request.user, form.cleaned_data["note"])
            messages.success(request, f"{row.delta:+d} credits. {company.name} now has {row.balance_after}.")
            return redirect("admin:accounts_company_change", pk)
        return render(request, "admin/accounts/adjust_credits.html", {
            **self.admin_site.each_context(request), "company": company, "form": form,
            "balance": credits.balance(company), "title": f"Adjust credits for {company.name}",
        })
