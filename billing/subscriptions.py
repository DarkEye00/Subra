"""Subscriptions billed by Paystack Plans (subscriptions spec, revision 2).

Paystack stores the card and charges it every month. SUBRA:
- opens checkouts (first payment, top-ups) with a Payment row committed before calling Paystack,
- settles paid card payments idempotently (billing.services.apply_event dispatches here),
- re-reads each subscription from Paystack's API whenever a subscription event arrives, rather than trusting the
  shape of the webhook body (sync_subscription),
- and runs grace, lapse and period-end itself (billing.tasks), so a Paystack outage never lapses anyone.
"""

import json
import logging
import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import Company, membership_for
from core.alerts import alert_admins

from . import credits, emails
from .models import CreditPack, Payment, Plan, Subscription, WebhookDelivery
from .providers import CallbackRejected, ProviderError, get_provider

log = logging.getLogger(__name__)
SS = Subscription.Status
CHECKOUT_REUSE = timedelta(minutes=30)  # an open checkout this recent is reused instead of replaced
CHECKOUT_GIVE_UP = timedelta(hours=24)


class BillingError(Exception):
    """Something the customer should see."""


def paystack():
    return get_provider("paystack")


def _owner_membership(user):
    membership = membership_for(user)
    if not membership.is_owner:
        raise BillingError("Only your company's owner can manage the plan.")
    return membership


def _check_enabled():
    if not settings.SUBSCRIPTIONS_ENABLED:
        raise BillingError("Plans aren't available yet.")


def starter_headroom_ok(amount_kes):
    """Paystack stops collecting at the Starter limit, which would stop M-Pesa deck payments too."""
    limit = settings.PAYSTACK_STARTER_LIMIT_KES
    if not limit:
        return True
    collected = Payment.objects.filter(provider="paystack", status__in=[Payment.Status.PAID, Payment.Status.REFUNDED])
    total = collected.aggregate(t=Sum("amount_kes"))["t"] or 0
    return total + amount_kes <= limit * 0.95


def live_subscription(company):
    return credits.live_subscription(company)


# --- opening checkouts ------------------------------------------------------------------------------------------

def _callback_url():
    return settings.SITE_URL + reverse("billing:card_return")


def _settle_stale_checkouts(company, provider):
    """Before opening a new checkout, ask Paystack about open ones: a customer may have paid in another tab."""
    from .services import check_payment

    for payment in Payment.objects.filter(company=company, status=Payment.Status.PENDING,
                                          kind=Payment.Kind.SUBSCRIPTION):
        check_payment(payment, provider=provider)


def start_subscription_checkout(user, plan, consent_text, provider=None, replace=False):
    """Open Paystack's checkout for a plan. Returns the URL to send the customer to.

    replace=True is for a plan change or "pay now with a new card": the new subscription takes over from the live
    one when its first payment succeeds, and the old one is ended then (its credits carry over)."""
    _check_enabled()
    membership = _owner_membership(user)
    company = membership.company
    provider = provider or paystack()
    if not consent_text:
        raise BillingError("Tick the box to agree to the monthly charge.")
    if not plan.active or not plan.paystack_plan_code:
        raise BillingError("This plan isn't available right now.")
    if plan.paystack_amount_kes != plan.price_kes:
        alert_admins(f"Plan {plan.code} is out of sync with Paystack",
                     f"{plan.name} costs KES {plan.price_kes} in SUBRA but KES {plan.paystack_amount_kes} at Paystack. "
                     "Run manage.py sync_paystack_plans.")
        raise BillingError("This plan is being updated. Please try again in a few minutes.")
    if not starter_headroom_ok(plan.price_kes):
        raise BillingError("Plans are paused for a few days. You can still pay per deck with M-Pesa.")
    _settle_stale_checkouts(company, provider)

    with transaction.atomic():
        company = credits.lock_company(company)
        live = live_subscription(company)
        if live is not None and not replace:
            raise BillingError("Your company already has a plan.")
        if replace and live is None:
            raise BillingError("Your company has no plan to change.")
        if replace and live.plan_id == plan.pk and live.status == SS.ACTIVE:
            raise BillingError(f"You're already on the {plan.name} plan.")
        open_sub = Subscription.objects.filter(company=company, status=SS.INCOMPLETE).first()
        if open_sub is not None:
            pending = open_sub.payments.filter(status=Payment.Status.PENDING).first()
            fresh = pending and timezone.now() - pending.created_at < CHECKOUT_REUSE
            if fresh and open_sub.plan_id == plan.pk and (pending.raw_init or {}).get("authorization_url"):
                return pending.raw_init["authorization_url"]
            _abandon(open_sub, "Replaced by a new checkout")
        sub = Subscription.objects.create(
            company=company, plan=plan, email=user.email, consent_text=consent_text, consented_at=timezone.now(),
            created_by=user, replaces=live if replace else None,
        )
        sub.log(f"Checkout opened by {user.email} for {plan.name} at KES {plan.price_kes}")
        reference = f"subra-sub-{company.pk}-{uuid.uuid4().hex[:8]}"
        payment = Payment.objects.create(
            kind=Payment.Kind.SUBSCRIPTION, channel=Payment.Channel.CARD, company=company, subscription=sub,
            provider="paystack", provider_ref=reference, reference=reference, amount_kes=plan.price_kes,
        )
    try:
        out = provider.initialize_checkout(
            email=user.email, amount_kes=plan.price_kes, reference=reference, callback_url=_callback_url(),
            channels=["card"], plan_code=plan.paystack_plan_code,
            metadata={"kind": "subscription", "company": company.pk, "subscription": sub.pk},
        )
    except ProviderError as e:
        log.warning("Subscription checkout failed for company %s: %s", company.pk, e)
        with transaction.atomic():
            _fail_payment(payment, "init", str(e))
            _abandon(Subscription.objects.select_for_update().get(pk=sub.pk), "Paystack refused the checkout")
        raise BillingError("We couldn't reach Paystack. Please try again in a minute.")
    Payment.objects.filter(pk=payment.pk).update(raw_init=out)
    return out["authorization_url"]


def start_topup_checkout(user, pack, provider=None):
    _check_enabled()
    company = _owner_membership(user).company
    provider = provider or paystack()
    if not pack.active:
        raise BillingError("This top-up isn't available right now.")
    if live_subscription(company) is None:
        raise BillingError("Top-ups are for companies with a plan. Pay per deck with M-Pesa instead.")
    if not starter_headroom_ok(pack.price_kes):
        raise BillingError("Top-ups are paused for a few days. You can still pay per deck with M-Pesa.")
    reference = f"subra-top-{company.pk}-{uuid.uuid4().hex[:8]}"
    payment = Payment.objects.create(
        kind=Payment.Kind.TOPUP, channel=Payment.Channel.CARD, company=company, credit_pack=pack,
        subscription=live_subscription(company), provider="paystack", provider_ref=reference, reference=reference,
        amount_kes=pack.price_kes,
    )
    try:
        out = provider.initialize_checkout(
            email=user.email, amount_kes=pack.price_kes, reference=reference, callback_url=_callback_url(),
            channels=["card", "mobile_money"], metadata={"kind": "topup", "company": company.pk, "pack": pack.pk},
        )
    except ProviderError as e:
        log.warning("Top-up checkout failed for company %s: %s", company.pk, e)
        _fail_payment(payment, "init", str(e))
        raise BillingError("We couldn't reach Paystack. Please try again in a minute.")
    Payment.objects.filter(pk=payment.pk).update(raw_init=out)
    return out["authorization_url"]


def card_return(user, reference, provider=None):
    """The customer came back from Paystack's page. Look the payment up in their own company only, then ask
    Paystack (nothing in the query string is trusted). Returns the payment, or None."""
    from .services import check_payment

    company = membership_for(user).company
    payment = Payment.objects.filter(company=company, reference=reference).exclude(kind=Payment.Kind.DECK).first()
    if payment is None:
        return None
    if payment.status == Payment.Status.PENDING:
        check_payment(payment, provider=provider or paystack())
        payment.refresh_from_db()
    return payment


def _fail_payment(payment, code, desc):
    Payment.objects.filter(pk=payment.pk, status=Payment.Status.PENDING).update(
        status=Payment.Status.FAILED, result_code=code[:32], result_desc=desc[:255], updated_at=timezone.now())


def _abandon(sub, note):
    for payment in sub.payments.filter(status=Payment.Status.PENDING):
        _fail_payment(payment, "abandoned", note)
    sub.transition(SS.ABANDONED, note=note, ended_at=timezone.now())


# --- settling card payments ---------------------------------------------------------------------------------------

def _transaction_data(event):
    raw = event.raw or {}
    data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
    return data if isinstance(data, dict) else {}


def _card_details(data):
    auth = data.get("authorization") or {}
    exp = ""
    if auth.get("exp_month") and auth.get("exp_year"):
        exp = f"{str(auth['exp_month']).zfill(2)}/{auth['exp_year']}"
    return {"card_last4": str(auth.get("last4") or "")[:4], "card_brand": str(auth.get("brand") or "")[:20],
            "card_exp": exp}


def _customer(data):
    customer = data.get("customer") or {}
    return str(customer.get("customer_code") or ""), str(customer.get("id") or "")


def settle_card_payment(payment, event):
    """Called by apply_event inside its transaction, with the payment locked, for a confirmed card payment of
    the expected amount. Idempotent: every credit write has its own key."""
    from .services import _fee

    data = _transaction_data(event)
    payment.status = Payment.Status.PAID
    payment.result_code, payment.result_desc = (event.code or "")[:32], (event.reason or "")[:255]
    if data.get("channel") == "mobile_money":
        payment.channel = Payment.Channel.MPESA
    if event.receipt:
        payment.provider_receipt = event.receipt
    payment.fee_kes, payment.fee_estimated = _fee(payment, event)
    payment.save()

    if payment.kind == Payment.Kind.TOPUP:
        row, created = credits.topup_for_payment(payment)
        return
    sub = Subscription.objects.select_for_update().get(pk=payment.subscription_id)
    customer_code, customer_id = _customer(data)
    details = {k: v for k, v in _card_details(data).items() if v}
    if customer_code:
        details.update(paystack_customer_code=customer_code, paystack_customer_id=customer_id)
    if payment.kind == Payment.Kind.SUBSCRIPTION:
        _activate(sub, payment, details)
    elif payment.kind == Payment.Kind.RENEWAL:
        _renewed(sub, payment, details)


def _activate(sub, payment, details):
    if sub.status in Subscription.LIVE:
        return  # a duplicate of a confirmation we already applied
    company = credits.lock_company(sub.company)
    live = live_subscription(company)
    if live is not None and live.pk != (sub.replaces_id or 0):
        payment.result_desc = "Company already has a live plan: refund needed"
        payment.save(update_fields=["result_desc", "updated_at"])
        alert_admins(f"Second subscription paid for {company.name}: refund and cancel it",
                     f"Payment {payment.pk} ({payment.reference}, KES {payment.amount_kes}) started a second plan "
                     f"while subscription {live.pk} is live. Cancel the new Paystack subscription and refund.")
        return
    if live is not None:  # a plan change or a new card: the old one ends, its credits carry over
        _end(live, SS.CANCELLED, f"Replaced by subscription {sub.pk}", expire=False)
        transaction.on_commit(lambda: disable_at_paystack(live.pk))
    if sub.status == SS.ABANDONED:  # paid after we gave up on the checkout: honour it
        for other in Subscription.objects.filter(company=company, status=SS.INCOMPLETE):
            _abandon(other, f"Superseded: subscription {sub.pk} was paid")
        Subscription.objects.filter(pk=sub.pk).update(status=SS.INCOMPLETE)
        sub.refresh_from_db()
    now = timezone.now()
    sub.transition(SS.ACTIVE, note=f"First payment {payment.reference} confirmed", periods_paid=1,
                   renews_at=sub.renews_at or now + timedelta(days=30), **details)
    row, _ = credits.grant_for_payment(payment, sub.plan)
    emails.send("subscription_active", sub.email, plan=sub.plan.name, granted=row.delta,
                balance=row.balance_after, price=f"{sub.plan.price_kes:,}", last4=sub.card_last4)
    transaction.on_commit(lambda: sync_subscription_safely(sub.pk))


def _renewed(sub, payment, details):
    if sub.status in Subscription.TERMINAL:
        payment.result_desc = f"Renewal for an ended plan ({sub.status}): refund needed"
        payment.save(update_fields=["result_desc", "updated_at"])
        alert_admins(f"Paystack renewed an ended plan for {sub.company.name}: refund it",
                     f"Payment {payment.pk} ({payment.reference}, KES {payment.amount_kes}) renewed subscription "
                     f"{sub.pk}, which is {sub.status}. Disable it on Paystack and refund the charge.")
        return
    fields = dict(details, periods_paid=sub.periods_paid + 1, past_due_since=None, grace_ends_at=None,
                  grace_warning_sent=False)
    to = SS.ACTIVE if sub.status in (SS.ACTIVE, SS.PAST_DUE) else sub.status
    sub.transition(to, note=f"Renewal {payment.reference} paid", **fields)
    credits.grant_for_payment(payment, sub.plan)
    transaction.on_commit(lambda: sync_subscription_safely(sub.pk))


def record_renewal(data, provider=None):
    """A Paystack-initiated charge (no reference of ours): find its subscription, record a Payment, settle it.
    Returns the payment, or None if it isn't one of our plans."""
    from .services import apply_event

    reference = str(data.get("reference") or "")
    if not reference:
        return None
    existing = Payment.objects.filter(provider="paystack", provider_ref=reference).first()
    sub = existing.subscription if existing else _subscription_for_charge(data)
    if sub is None:
        return None
    if existing is None:
        amount = int(Decimal(str(data.get("amount") or 0)) / 100)
        try:
            with transaction.atomic():
                existing = Payment.objects.create(
                    kind=Payment.Kind.RENEWAL, channel=Payment.Channel.CARD, company=sub.company, subscription=sub,
                    provider="paystack", provider_ref=reference, reference=reference, amount_kes=amount,
                )
        except IntegrityError:  # the same renewal arriving twice at once
            existing = Payment.objects.get(provider="paystack", provider_ref=reference)
    provider = provider or paystack()
    return apply_event("paystack", provider.event_for(data, raw={"data": data}), source="callback")


def _subscription_for_charge(data):
    plan_code = str((data.get("plan") or {}).get("plan_code") or "") if isinstance(data.get("plan"), dict) else ""
    customer_code, _ = _customer(data)
    if not customer_code:
        return None
    subs = Subscription.objects.filter(paystack_customer_code=customer_code).exclude(status=SS.INCOMPLETE)
    if plan_code:
        subs = subs.filter(plan__paystack_plan_code=plan_code)
    live = subs.filter(status__in=Subscription.LIVE).first()
    return live or (subs.order_by("-created_at").first() if plan_code else None)


# --- reading subscriptions back from Paystack ------------------------------------------------------------------

def sync_subscription_safely(sub_pk, provider=None):
    try:
        return sync_subscription(Subscription.objects.get(pk=sub_pk), provider=provider)
    except ProviderError as e:
        log.warning("Couldn't sync subscription %s from Paystack: %s", sub_pk, e)
        return None


def _discover_code(sub, provider):
    """The first payment created a Paystack subscription; find its code by customer and plan."""
    if not sub.paystack_customer_id:
        return ""
    for item in provider.list_subscriptions(sub.paystack_customer_id):
        plan = item.get("plan") or {}
        if plan.get("plan_code") == sub.plan.paystack_plan_code and item.get("subscription_code"):
            taken = Subscription.objects.filter(paystack_subscription_code=item["subscription_code"]).exclude(
                pk=sub.pk).exists()
            if not taken:
                return item["subscription_code"]
    return ""


def sync_subscription(sub, provider=None, now=None):
    """Re-read a subscription from Paystack and apply what it says. Raises ProviderError when Paystack can't be
    reached: callers treat that as "unknown", never as a lapse. Never ends a subscription: only the hourly job
    does that, after grace."""
    provider = provider or paystack()
    now = now or timezone.now()
    code = sub.paystack_subscription_code or _discover_code(sub, provider)
    if not code:
        return sub
    data = provider.fetch_subscription(code)
    status = str(data.get("status") or "")
    next_date = parse_datetime(str(data.get("next_payment_date") or "")) if data.get("next_payment_date") else None
    with transaction.atomic():
        sub = Subscription.objects.select_for_update().get(pk=sub.pk)
        fields = {"paystack_subscription_code": code, "last_synced_at": now, "raw": data}
        if data.get("email_token"):
            fields["paystack_email_token"] = str(data["email_token"])
        fields.update({k: v for k, v in _card_details(data).items() if v})
        renewed = next_date is not None and sub.renews_at is not None and next_date > sub.renews_at
        if next_date is not None and (status == "active" or sub.renews_at is None):
            fields["renews_at"] = next_date
        if sub.status in Subscription.TERMINAL or sub.status == SS.INCOMPLETE:
            for k, v in fields.items():
                setattr(sub, k, v)
            sub.save()
            return sub
        if status == "attention":
            if sub.status == SS.ACTIVE:
                _start_grace(sub, now, fields)
                return sub
        elif status == "non-renewing" or status in ("cancelled", "complete", "completed"):
            if sub.status == SS.ACTIVE:
                sub.transition(SS.CANCELLING, note=f"Paystack reports {status}", cancelled_at=now, **fields)
                emails.send("cancelled", sub.email, plan=sub.plan.name, until=sub.renews_at)
                return sub
        elif status == "active":
            if sub.status == SS.PAST_DUE and renewed:
                sub.transition(SS.ACTIVE, note="Paystack reports the plan paid up again", past_due_since=None,
                               grace_ends_at=None, grace_warning_sent=False, **fields)
                return sub
        for k, v in fields.items():
            setattr(sub, k, v)
        sub.save()
    return sub


def _start_grace(sub, now, fields=None):
    until = now + timedelta(days=settings.SUBSCRIPTION_GRACE_DAYS)
    sub.transition(SS.PAST_DUE, note="Renewal charge failed", past_due_since=now, grace_ends_at=until,
                   **(fields or {}))
    emails.send("renewal_failed", sub.email, plan=sub.plan.name, last4=sub.card_last4,
                balance=credits.balance(sub.company), until=until)


def mark_past_due(sub, now=None):
    now = now or timezone.now()
    with transaction.atomic():
        sub = Subscription.objects.select_for_update().get(pk=sub.pk)
        if sub.status == SS.ACTIVE:
            _start_grace(sub, now)
    return sub


# --- ending, cancelling, resuming ---------------------------------------------------------------------------------

def _end(sub, to, note, expire=True, reason=""):
    sub.transition(to, note=note, ended_at=timezone.now())
    if expire:
        row = credits.expire_balance(sub.company, sub, note)
        if row is not None:
            emails.send("credits_expired", sub.email, plan=sub.plan.name, expired=-row.delta, reason=reason)
    return sub


def disable_at_paystack(sub_pk, provider=None):
    """Stop Paystack charging this subscription. A failure alerts: the customer could be charged again."""
    sub = Subscription.objects.get(pk=sub_pk)
    if not sub.paystack_subscription_code:
        return True
    provider = provider or paystack()
    try:
        if not sub.paystack_email_token:
            sync_subscription(sub, provider=provider)
            sub.refresh_from_db()
        provider.disable_subscription(sub.paystack_subscription_code, sub.paystack_email_token)
    except ProviderError as e:
        alert_admins(f"Couldn't disable Paystack subscription {sub.paystack_subscription_code}",
                     f"Subscription {sub.pk} for {sub.company.name} has ended in SUBRA, but Paystack refused to "
                     f"disable it ({e}). Disable it in the Paystack dashboard so the card isn't charged again.")
        return False
    sub.log("Disabled on Paystack")
    return True


def cancel(user, provider=None):
    """No further charges; credits stay until the end of the paid period."""
    _check_enabled()
    company = _owner_membership(user).company
    sub = live_subscription(company)
    if sub is None or sub.status == SS.CANCELLING:
        raise BillingError("There's no plan to cancel.")
    provider = provider or paystack()
    if not disable_at_paystack(sub.pk, provider=provider):
        raise BillingError("We couldn't reach Paystack to cancel. Please try again in a minute.")
    with transaction.atomic():
        sub = Subscription.objects.select_for_update().get(pk=sub.pk)
        if sub.status == SS.PAST_DUE:  # unpaid: nothing more is owed, credits last until the grace ends
            sub.transition(SS.CANCELLING, note=f"Cancelled by {user.email} during grace", cancelled_at=timezone.now(),
                           renews_at=sub.grace_ends_at)
        else:
            sub.transition(SS.CANCELLING, note=f"Cancelled by {user.email}", cancelled_at=timezone.now())
        emails.send("cancelled", sub.email, plan=sub.plan.name, until=sub.renews_at)
    return sub


def resume(user, provider=None):
    _check_enabled()
    company = _owner_membership(user).company
    sub = live_subscription(company)
    if sub is None or sub.status != SS.CANCELLING or (sub.renews_at and sub.renews_at <= timezone.now()):
        raise BillingError("There's no cancelled plan to resume.")
    if sub.past_due_since:
        raise BillingError("Your last card payment failed, so start the plan again with a card.")
    provider = provider or paystack()
    try:
        provider.enable_subscription(sub.paystack_subscription_code, sub.paystack_email_token)
    except ProviderError as e:
        log.warning("Resume failed for subscription %s: %s", sub.pk, e)
        raise BillingError("We couldn't reach Paystack to resume. Please try again in a minute.")
    with transaction.atomic():
        sub = Subscription.objects.select_for_update().get(pk=sub.pk)
        sub.transition(SS.ACTIVE, note=f"Resumed by {user.email}", cancelled_at=None)
    sync_subscription_safely(sub.pk, provider=provider)
    return sub


def card_update_link(user, provider=None):
    _check_enabled()
    sub = live_subscription(_owner_membership(user).company)
    if sub is None or not sub.paystack_subscription_code:
        raise BillingError("There's no card on file to change.")
    try:
        return (provider or paystack()).manage_link(sub.paystack_subscription_code)
    except ProviderError as e:
        log.warning("Manage link failed for subscription %s: %s", sub.pk, e)
        raise BillingError("We couldn't reach Paystack. Please try again in a minute.")


# --- webhooks -----------------------------------------------------------------------------------------------------

SUBSCRIPTION_EVENTS = {"subscription.create", "subscription.not_renew", "subscription.disable",
                       "subscription.expiring_cards", "invoice.create", "invoice.update", "invoice.payment_failed"}


def _sub_code(data):
    if data.get("subscription_code"):
        return str(data["subscription_code"])
    nested = data.get("subscription")
    if isinstance(nested, dict) and nested.get("subscription_code"):
        return str(nested["subscription_code"])
    return ""


def _subscription_for_event(data):
    code = _sub_code(data)
    if code:
        sub = Subscription.objects.filter(paystack_subscription_code=code).first()
        if sub is not None:
            return sub
    customer_code, customer_id = _customer(data)
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    if not customer_code:
        return None
    subs = Subscription.objects.filter(paystack_customer_code=customer_code, paystack_subscription_code="")
    if plan.get("plan_code"):
        subs = subs.filter(plan__paystack_plan_code=plan["plan_code"])
    sub = subs.exclude(status__in=Subscription.TERMINAL).order_by("-created_at").first()
    if sub is not None and code:
        Subscription.objects.filter(pk=sub.pk).update(paystack_subscription_code=code)
        sub.refresh_from_db()
    return sub


def handle_paystack_webhook(body, headers, provider=None):
    """The Paystack webhook: authenticate, store, dispatch. Raises CallbackRejected or ValueError like
    billing.services.handle_callback. Returns a short outcome string."""
    from .services import apply_event

    provider = provider or paystack()
    provider.verify_signature(body, headers)
    try:
        payload = json.loads(body)
    except ValueError:
        raise ValueError("Not JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Not a Paystack event")
    event_type, data = str(payload.get("event") or ""), payload["data"]
    reference = str(data.get("reference") or _sub_code(data) or "")[:100]
    delivery = WebhookDelivery.objects.create(provider="paystack", event_type=event_type[:60], reference=reference,
                                              body=payload)
    outcome = _dispatch(event_type, data, payload, provider, apply_event)
    WebhookDelivery.objects.filter(pk=delivery.pk).update(outcome=outcome[:200])
    return outcome


def _dispatch(event_type, data, payload, provider, apply_event):
    if event_type == "charge.success":
        reference = str(data.get("reference") or "")
        known = Payment.objects.filter(provider="paystack", provider_ref=reference).exists()
        if known or reference.startswith("subra-"):
            apply_event("paystack", provider.event_for(data, raw=payload), source="callback")
            return "applied to payment"
        if record_renewal(data, provider=provider) is not None:
            return "renewal recorded"
        if not data.get("plan"):
            return "ignored: not a SUBRA charge"  # e.g. a payment link on the same Paystack account
        alert_admins("Paystack charge for no known plan",
                     f"Paystack sent charge.success for reference {reference} (KES "
                     f"{Decimal(str(data.get('amount') or 0)) / 100}), which matches no SUBRA payment or subscription. "
                     "Check the Paystack dashboard.")
        return "unmatched charge"
    if event_type in SUBSCRIPTION_EVENTS:
        sub = _subscription_for_event(data)
        if sub is None:
            if event_type == "subscription.create":
                alert_admins("Paystack subscription with no SUBRA record",
                             f"Paystack created subscription {_sub_code(data) or '(no code)'} that SUBRA can't match "
                             "to a company. Check it in the Paystack dashboard.")
            return "no matching subscription"
        if event_type == "invoice.payment_failed":
            mark_past_due(sub)
        try:
            sync_subscription(sub, provider=provider)
        except ProviderError as e:
            log.warning("Sync after %s failed for subscription %s: %s", event_type, sub.pk, e)
            return f"{event_type}: sync failed, will retry hourly"
        return f"{event_type}: synced subscription {sub.pk}"
    if event_type in ("refund.processed", "refund.failed"):
        return _refund_event(event_type, data)
    if event_type.startswith("charge.dispute"):
        alert_admins(f"Paystack dispute ({event_type})",
                     f"A customer disputed a charge. Reference: {_refund_reference(data) or 'unknown'}. Respond in the "
                     "Paystack dashboard before the deadline.")
        return "dispute alerted"
    return "ignored"


def _refund_reference(data):
    for key in ("transaction_reference", "reference"):
        if data.get(key):
            return str(data[key])
    nested = data.get("transaction")
    if isinstance(nested, dict) and nested.get("reference"):
        return str(nested["reference"])
    return ""


def _refund_event(event_type, data):
    reference = _refund_reference(data)
    payment = Payment.objects.filter(provider="paystack", reference=reference).first() if reference else None
    if payment is None:
        return "refund for an unknown payment"
    if event_type == "refund.failed":
        alert_admins(f"Paystack refund failed for payment {payment.pk}", f"Reference {reference}. Check the dashboard.")
        return "refund failed alerted"
    mark_refunded(payment)
    return "refund applied"


def mark_refunded(payment):
    """The money went back: the payment is refunded and, for card payments, what it granted is clawed back."""
    with transaction.atomic():
        payment = Payment.objects.select_for_update().get(pk=payment.pk)
        if payment.status == Payment.Status.PAID:
            payment.status, payment.refunded_at = Payment.Status.REFUNDED, timezone.now()
            payment.save(update_fields=["status", "refunded_at", "updated_at"])
    if payment.kind != Payment.Kind.DECK:
        credits.clawback_payment(payment)
    return payment


# --- plans ------------------------------------------------------------------------------------------------------

def sync_plans(provider=None, stdout=None):
    """Create each active plan at Paystack, or update its amount there. Returns [(plan, action)]."""
    provider = provider or paystack()
    out = []
    for plan in Plan.objects.all():
        name = f"SUBRA {plan.name}"
        if not plan.paystack_plan_code:
            if not plan.active:
                continue
            data = provider.create_plan(name, plan.price_kes)
            plan.paystack_plan_code = str(data.get("plan_code") or "")
            action = "created"
        elif plan.paystack_amount_kes != plan.price_kes:
            provider.update_plan(plan.paystack_plan_code, name, plan.price_kes)
            action = "updated"
        else:
            out.append((plan, "unchanged"))
            continue
        plan.paystack_amount_kes = plan.price_kes
        plan.save(update_fields=["paystack_plan_code", "paystack_amount_kes"])
        out.append((plan, action))
    return out


def company_summary(company):
    """What the plan page and the nav pill show."""
    sub = live_subscription(company)
    return {"subscription": sub, "balance": credits.balance(company),
            "ever_subscribed": sub is not None or Subscription.objects.filter(company=company).exclude(
                status__in=[SS.INCOMPLETE, SS.ABANDONED]).exists()}


__all__ = ["BillingError", "CallbackRejected", "Company", "CreditPack"]
