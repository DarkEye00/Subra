import csv
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.db import transaction
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.ratelimit import allow

from . import companies
from .forms import InviteSignupForm, SignupForm
from .models import membership_for


def signup(request):
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            if next_url and url_has_allowed_host_and_scheme(next_url, {request.get_host()}, request.is_secure()):
                return redirect(next_url)
            return redirect("home")
    else:
        form = SignupForm()
    return render(request, "accounts/signup.html", {"form": form, "next": next_url})


# --- plans and credits (subscriptions spec, section 8) ----------------------------------------------------------

def _subscriptions_on():
    if not settings.SUBSCRIPTIONS_ENABLED:
        raise Http404


def _limited(request, action="billing"):
    limit = settings.INVITE_RATE_LIMIT if action == "invite" else settings.BILLING_RATE_LIMIT
    if allow(request, action, *limit):
        return False
    messages.error(request, "Too many attempts. Please wait a few minutes and try again.")
    return True


def consent_text(plan):
    return (f"I agree that SUBRA charges KES {plan.price_kes:,} to my card every month for the {plan.name} plan "
            "until I cancel.")


def _dev_billing():
    """The simulated subscription: DEBUG only, and only while Paystack has no keys."""
    from billing.subscriptions import paystack

    return settings.DEBUG and not paystack().configured


def plans(request):
    from billing import credits
    from billing.models import Plan, PriceTier, ProductType

    _subscriptions_on()
    tiers = list(PriceTier.objects.filter(active=True, product_type=ProductType.DECK_POLISH, credits__gt=0))
    rows = []
    for plan in Plan.offered_to(request.user):
        decks = [(t.name, plan.monthly_credits // t.credits) for t in tiers]
        rows.append({"plan": plan, "decks": decks, "consent": consent_text(plan)})
    context = {"rows": rows, "grace_days": settings.SUBSCRIPTION_GRACE_DAYS,
               "carryover": settings.CREDIT_CARRYOVER_MULTIPLE, "live": None, "is_owner": True,
               "dev_billing": _dev_billing()}
    if request.user.is_authenticated:
        membership = membership_for(request.user)
        context["live"] = credits.live_subscription(membership.company)
        context["is_owner"] = membership.is_owner
    return render(request, "accounts/plans.html", context)


@login_required
@require_POST
def subscribe(request):
    from billing import subscriptions
    from billing.models import Plan

    _subscriptions_on()
    if _limited(request):
        return redirect("accounts:plans")
    plan = Plan.offered_to(request.user).filter(code=request.POST.get("plan")).first()
    if plan is None:
        messages.error(request, "Choose a plan.")
        return redirect("accounts:plans")
    agreed = consent_text(plan) if request.POST.get("consent") else ""
    replace = subscriptions.live_subscription(membership_for(request.user).company) is not None
    try:
        url = subscriptions.start_subscription_checkout(request.user, plan, agreed, replace=replace)
    except subscriptions.BillingError as e:
        messages.error(request, str(e))
        return redirect("accounts:plan" if replace else "accounts:plans")
    return redirect(url)


@login_required
def plan(request):
    from billing import credits
    from billing.models import CreditPack, CreditTransaction, Plan, Subscription
    from orders.models import Order

    _subscriptions_on()
    membership = membership_for(request.user)
    company = membership.company
    sub = credits.live_subscription(company)
    since = credits.period_start(sub)
    usage = credits.usage_by_member(company, since=since)
    context = {
        "membership": membership, "company": company, "sub": sub, "balance": credits.balance(company),
        "is_owner": membership.is_owner, "usage": usage, "dev_billing": _dev_billing() and sub is not None,
        "grace_days": settings.SUBSCRIPTION_GRACE_DAYS,
    }
    if membership.is_owner:
        orders = Order.objects.filter(company=company).select_related("user").order_by("-created_at")[:50]
        context.update(
            members=companies.members(company), invites=companies.open_invites(company),
            usage_all=credits.usage_by_member(company), orders=orders,
            ledger=CreditTransaction.objects.filter(company=company).select_related("user")[:30],
            packs=CreditPack.objects.filter(active=True) if sub is not None else [],
            other_plans=[(p, consent_text(p)) for p in Plan.offered_to(request.user)
                         if sub is None or p.pk != sub.plan_id],
            past=Subscription.objects.filter(company=company, status__in=Subscription.TERMINAL).exclude(
                status=Subscription.Status.ABANDONED).first(),
            cap=sub.plan.carryover_cap if sub else 0,
        )
        if sub is not None:
            context["next_grant"] = credits.grant_amount(sub.plan.monthly_credits, context["balance"])
    else:
        context["my_usage"] = next((u for u in usage if u[0] == request.user.email), None)
    return render(request, "accounts/plan.html", context)


@login_required
@require_POST
def plan_action(request):
    """Cancel, resume, change card, or pay now with a new card."""
    from billing import subscriptions

    _subscriptions_on()
    if _limited(request):
        return redirect("accounts:plan")
    action = request.POST.get("action")
    try:
        if action == "cancel":
            sub = subscriptions.cancel(request.user)
            messages.success(request, f"Cancelled. Your card won't be charged again; credits work until "
                                      f"{timezone.localtime(sub.renews_at):%-d %b %Y}.")
        elif action == "resume":
            subscriptions.resume(request.user)
            messages.success(request, "Your plan is back on. It renews as before.")
        elif action == "card":
            return redirect(subscriptions.card_update_link(request.user))
        elif action == "rename":
            companies.rename(request.user, request.POST.get("name", ""))
            messages.success(request, "Company renamed.")
        elif action == "paynow":
            sub = subscriptions.live_subscription(membership_for(request.user).company)
            if sub is None or sub.status != sub.Status.PAST_DUE:
                raise subscriptions.BillingError("There's no overdue payment.")
            return redirect(subscriptions.start_subscription_checkout(
                request.user, sub.plan, consent_text(sub.plan) if request.POST.get("consent") else "", replace=True))
        else:
            raise Http404
    except (subscriptions.BillingError, companies.CompanyError) as e:
        messages.error(request, str(e))
    return redirect("accounts:plan")


@login_required
@require_POST
def topup(request):
    from billing import subscriptions
    from billing.models import CreditPack

    _subscriptions_on()
    if _limited(request):
        return redirect("accounts:plan")
    pack = CreditPack.objects.filter(pk=request.POST.get("pack"), active=True).first()
    if pack is None:
        messages.error(request, "Choose a top-up.")
        return redirect("accounts:plan")
    try:
        return redirect(subscriptions.start_topup_checkout(request.user, pack))
    except subscriptions.BillingError as e:
        messages.error(request, str(e))
        return redirect("accounts:plan")


@login_required
def usage_csv(request):
    from billing.models import CreditTransaction

    _subscriptions_on()
    membership = membership_for(request.user)
    if not membership.is_owner:
        raise Http404
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="subra-credits.csv"'
    writer = csv.writer(response)
    writer.writerow(["date", "kind", "credits", "balance", "member", "deck", "note"])
    rows = CreditTransaction.objects.filter(company=membership.company).select_related("user", "order")
    for row in rows.order_by("id"):
        writer.writerow([timezone.localtime(row.created_at).strftime("%Y-%m-%d %H:%M"), row.get_kind_display(),
                         row.delta, row.balance_after, row.user.email if row.user_id else "",
                         row.order.original_name if row.order_id else "", row.note])
    return response


# --- members and invites ---------------------------------------------------------------------------------------

@login_required
@require_POST
def invite(request):
    _subscriptions_on()
    if _limited(request, "invite"):
        return redirect("accounts:plan")
    try:
        inv, _ = companies.create_invite(request.user, request.POST.get("email", ""))
        messages.success(request, f"Invitation sent to {inv.email}.")
    except companies.CompanyError as e:
        messages.error(request, str(e))
    return redirect("accounts:plan")


@login_required
@require_POST
def revoke_invite(request, invite_id):
    _subscriptions_on()
    try:
        companies.revoke_invite(request.user, invite_id)
        messages.success(request, "Invitation withdrawn.")
    except companies.CompanyError as e:
        messages.error(request, str(e))
    return redirect("accounts:plan")


@login_required
@require_POST
def remove_member(request, user_id):
    _subscriptions_on()
    try:
        removed = companies.remove_member(request.user, user_id)
        messages.success(request, f"{removed.email} was removed. Their decks here stay with the company.")
    except companies.CompanyError as e:
        messages.error(request, str(e))
    return redirect("accounts:plan")


def invite_accept(request, token):
    """Accept an invitation: sign in as the invited email, or create an account with that email."""
    _subscriptions_on()
    found = companies.find_invite(token)
    if found is None:
        return render(request, "accounts/invite.html", {"invalid": True}, status=404)
    context = {"invite": found, "token": token}
    if request.user.is_authenticated:
        if request.method == "POST":
            try:
                companies.accept_invite(request.user, found)
            except companies.CompanyError as e:
                messages.error(request, str(e))
                return redirect("accounts:invite_accept", token=token)
            messages.success(request, f"You've joined {found.company.name}.")
            return redirect("accounts:plan")
        context["reason"] = companies.why_cant_join(request.user, found)
        return render(request, "accounts/invite.html", context)
    from .models import User

    if User.objects.filter(email__iexact=found.email).exists():
        context["sign_in"] = True
        return render(request, "accounts/invite.html", context)
    form = InviteSignupForm(found.email, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        companies.accept_invite(user, found)
        messages.success(request, f"Welcome. You've joined {found.company.name}.")
        return redirect("accounts:plan")
    context["form"] = form
    return render(request, "accounts/invite.html", context)


# --- development only -------------------------------------------------------------------------------------------

@login_required
@require_POST
def dev_subscribe(request):
    """DEBUG only, while Paystack has no keys: pretend a subscription was paid, or that its renewal failed."""
    from billing import credits, subscriptions
    from billing.models import Payment, Plan, Subscription

    _subscriptions_on()
    if not _dev_billing():
        raise Http404
    membership = membership_for(request.user)
    if not membership.is_owner:
        raise Http404
    company = membership.company
    sub = credits.live_subscription(company)
    what = request.POST.get("what", "subscribe")
    if what == "fail" and sub is not None:
        subscriptions.mark_past_due(sub)
        return redirect("accounts:plan")
    if what == "renew" and sub is not None:
        ref = f"dev-rnw-{sub.pk}-{sub.periods_paid + 1}-{timezone.now():%H%M%S}"
        payment = Payment.objects.create(kind=Payment.Kind.RENEWAL, channel=Payment.Channel.CARD, company=company,
                                         subscription=sub, provider_ref=ref, reference=ref,
                                         amount_kes=sub.plan.price_kes, status=Payment.Status.PAID)
        with transaction.atomic():
            subscriptions._renewed(Subscription.objects.select_for_update().get(pk=sub.pk), payment,
                                   {"renews_at": sub.renews_at + timedelta(days=30)})
        return redirect("accounts:plan")
    plan_obj = Plan.objects.filter(code=request.POST.get("plan"), active=True).first() or Plan.objects.first()
    if sub is not None:
        messages.error(request, "Your company already has a plan.")
        return redirect("accounts:plan")
    sub = Subscription.objects.create(company=company, plan=plan_obj, email=request.user.email,
                                      consent_text=consent_text(plan_obj), consented_at=timezone.now(),
                                      created_by=request.user)
    ref = f"dev-sub-{sub.pk}"
    payment = Payment.objects.create(kind=Payment.Kind.SUBSCRIPTION, channel=Payment.Channel.CARD, company=company,
                                     subscription=sub, provider_ref=ref, reference=ref, amount_kes=plan_obj.price_kes,
                                     status=Payment.Status.PAID)
    with transaction.atomic():
        subscriptions._activate(Subscription.objects.select_for_update().get(pk=sub.pk), payment,
                                {"card_last4": "4081", "card_brand": "visa", "card_exp": "12/2030"})
    messages.success(request, f"Development: {plan_obj.name} plan simulated.")
    return redirect("accounts:plan")

