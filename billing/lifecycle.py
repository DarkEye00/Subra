"""The subscription jobs SUBRA runs itself (subscriptions spec, sections 3 and 7).

Paystack charges cards; SUBRA decides when credits stop. Every job takes an injected `now`, so tests and
`manage.py billing_tick --now ...` drive the same code as Celery beat.

A system failure never lapses anyone: before lapsing, the subscription is re-read from Paystack, and if that read
fails the lapse waits for the next run. RENEWALS_PAUSED stops every lapse and every expiry.
"""

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from core.alerts import alert_admins

from . import credits, emails
from .models import Payment, Plan, Subscription, WebhookDelivery
from .providers import ProviderError
from .subscriptions import _abandon, _end, disable_at_paystack, paystack, record_renewal, sync_subscription

log = logging.getLogger(__name__)
SS = Subscription.Status
MISSED_RENEWAL_AFTER = timedelta(hours=6)
RENEWAL_ALERT_AFTER = timedelta(hours=48)


def run_hourly(now=None, provider=None):
    now = now or timezone.now()
    provider = provider or paystack()
    done = {
        "lapsed": lapse_overdue(now, provider),
        "ended": end_cancelled_periods(now),
        "abandoned": abandon_stale_checkouts(now, provider),
        "renewals_found": catch_missed_renewals(now, provider),
        "grace_warnings": warn_last_day_of_grace(now),
    }
    log.info("Subscriptions hourly: %s", done)
    return done


def run_daily(now=None):
    now = now or timezone.now()
    done = {
        "reminders": send_renewal_reminders(now),
        "ledger_mismatches": check_ledger(),
        "deliveries_pruned": prune_webhook_deliveries(now),
        "plans_out_of_sync": check_plans(),
    }
    log.info("Billing daily: %s", done)
    return done


def lapse_overdue(now, provider):
    if settings.RENEWALS_PAUSED:
        return 0
    lapsed, unreachable = 0, []
    for sub in Subscription.objects.filter(status=SS.PAST_DUE, grace_ends_at__lte=now).select_related("plan"):
        try:
            sub = sync_subscription(sub, provider=provider, now=now)
        except ProviderError as e:
            unreachable.append(f"{sub.pk}: {e}")
            continue
        if sub.status != SS.PAST_DUE:
            continue  # paid up after all
        disable_at_paystack(sub.pk, provider=provider)
        with transaction.atomic():
            sub = Subscription.objects.select_for_update().get(pk=sub.pk)
            if sub.status == SS.PAST_DUE:
                _end(sub, SS.LAPSED, "Grace ended without a successful payment", reason="the card payment wasn't fixed")
                lapsed += 1
    if unreachable:
        alert_admins("Couldn't check overdue subscriptions with Paystack",
                     "These subscriptions are past their grace period but Paystack couldn't be reached, so none was "
                     "lapsed. The next hourly run tries again.\n" + "\n".join(unreachable))
    return lapsed


def end_cancelled_periods(now):
    if settings.RENEWALS_PAUSED:
        return 0
    ended = 0
    for sub in Subscription.objects.filter(status=SS.CANCELLING, renews_at__lte=now):
        with transaction.atomic():
            sub = Subscription.objects.select_for_update().get(pk=sub.pk)
            if sub.status == SS.CANCELLING:
                _end(sub, SS.CANCELLED, "Cancelled; the paid period ended", reason="you cancelled it")
                ended += 1
    return ended


def abandon_stale_checkouts(now, provider):
    from .services import check_payment

    count = 0
    stale = Subscription.objects.filter(status=SS.INCOMPLETE, created_at__lt=now - timedelta(hours=24))
    for sub in stale:
        for payment in sub.payments.filter(status=Payment.Status.PENDING):
            check_payment(payment, provider=provider)  # paid at the last minute: this activates it
        with transaction.atomic():
            sub = Subscription.objects.select_for_update().get(pk=sub.pk)
            if sub.status == SS.INCOMPLETE:
                _abandon(sub, "Checkout not completed within 24 hours")
                count += 1
    return count


def catch_missed_renewals(now, provider):
    """Paystack should have charged at renews_at. If no renewal arrived, look for the charge ourselves (a lost
    webhook), then re-read the subscription. Alert once if still nothing after two days."""
    found = 0
    due = Subscription.objects.filter(status=SS.ACTIVE, renews_at__lt=now - MISSED_RENEWAL_AFTER)
    for sub in due.select_related("plan"):
        overdue = now - sub.renews_at
        try:
            if sub.paystack_customer_id:
                for tx in provider.list_transactions(sub.paystack_customer_id, since=sub.renews_at - timedelta(days=1)):
                    if tx.get("status") == "success" and record_renewal(tx, provider=provider) is not None:
                        found += 1
            sync_subscription(sub, provider=provider, now=now)
        except ProviderError as e:
            log.warning("Missed-renewal check failed for subscription %s: %s", sub.pk, e)
        sub.refresh_from_db()
        if sub.status == SS.ACTIVE and sub.renews_at < now - MISSED_RENEWAL_AFTER \
                and RENEWAL_ALERT_AFTER <= overdue < RENEWAL_ALERT_AFTER + timedelta(hours=1):
            alert_admins(f"Renewal didn't happen for {sub.company.name}",
                         f"Subscription {sub.pk} ({sub.paystack_subscription_code}) was due on "
                         f"{sub.renews_at:%d %b %Y %H:%M} and Paystack has reported no charge, success or failure. "
                         "Check it in the Paystack dashboard.")
    return found


def warn_last_day_of_grace(now):
    sent = 0
    soon = Subscription.objects.filter(status=SS.PAST_DUE, grace_warning_sent=False,
                                       grace_ends_at__lte=now + timedelta(days=1), grace_ends_at__gt=now)
    for sub in soon.select_related("plan", "company"):
        emails.send("grace_last_day", sub.email, plan=sub.plan.name, balance=credits.balance(sub.company),
                    until=sub.grace_ends_at)
        Subscription.objects.filter(pk=sub.pk).update(grace_warning_sent=True)
        sent += 1
    return sent


def send_renewal_reminders(now):
    """Three days before each charge (the recurring-consent promise)."""
    sent = 0
    due = Subscription.objects.filter(status=SS.ACTIVE, renews_at__gt=now + timedelta(days=2),
                                      renews_at__lte=now + timedelta(days=3))
    for sub in due.select_related("plan"):
        if sub.reminder_sent_for == sub.renews_at:
            continue
        local = timezone.localtime(sub.renews_at)
        emails.send("renewal_reminder", sub.email, plan=sub.plan.name, date=f"{local:%-d %b %Y}",
                    price=f"{sub.plan.price_kes:,}", last4=sub.card_last4)
        Subscription.objects.filter(pk=sub.pk).update(reminder_sent_for=sub.renews_at)
        sent += 1
    return sent


def check_ledger():
    mismatches = credits.ledger_mismatches()
    if mismatches:
        alert_admins("Credits ledger mismatch",
                     "These companies' balances don't equal the sum of their ledger rows (company, sum, balance):\n"
                     + "\n".join(str(m) for m in mismatches))
    return len(mismatches)


def prune_webhook_deliveries(now):
    deleted, _ = WebhookDelivery.objects.filter(created_at__lt=now - timedelta(days=settings.WEBHOOK_RETENTION_DAYS)
                                                ).delete()
    return deleted


def check_plans():
    mode = paystack().mode
    stale = [p.name for p in Plan.objects.filter(active=True)
             if p.paystack_amount_kes != p.price_kes or p.paystack_mode != mode]
    if stale and settings.SUBSCRIPTIONS_ENABLED:
        alert_admins("Plans not synced to Paystack",
                     f"{', '.join(stale)}: not set up at Paystack with the current ({mode or 'no'}) key, or the price "
                     "differs, so new subscribers are refused. "
                     "Run manage.py sync_paystack_plans.")
    return len(stale)
