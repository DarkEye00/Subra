"""The jobs SUBRA runs around Paystack's billing: grace, lapse, period end, missed renewals, reminders."""

from datetime import timedelta

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from billing import credits, lifecycle, subscriptions
from billing.models import Payment, Subscription, WebhookDelivery

from .test_subscriptions import FakePaystack, paystack_sub, transaction

SS = Subscription.Status


@pytest.fixture
def fake(monkeypatch):
    fake = FakePaystack()
    fake.subs["SUB_team1"] = paystack_sub(code="SUB_team1")
    monkeypatch.setattr(subscriptions, "paystack", lambda: fake)
    monkeypatch.setattr(lifecycle, "paystack", lambda: fake)
    return fake


@pytest.fixture
def funded(subs_on, subscribed, fund, on_commit):
    fund(50)
    mail.outbox.clear()
    return subscribed


def overdue(sub, days_ago=1):
    sub.status, sub.past_due_since = SS.PAST_DUE, timezone.now() - timedelta(days=7 + days_ago)
    sub.grace_ends_at = timezone.now() - timedelta(days=days_ago)
    sub.save()
    return sub


def test_grace_over_lapses_and_expires_the_credits(funded, fake, company, settings):
    overdue(funded)
    fake.subs["SUB_team1"] = paystack_sub(code="SUB_team1", status="attention")
    assert lifecycle.run_hourly(provider=fake)["lapsed"] == 1
    funded.refresh_from_db()
    assert funded.status == SS.LAPSED and funded.ended_at
    assert credits.balance(company) == 0
    assert ("POST", "/subscription/disable", {"code": "SUB_team1", "token": "tok_1"}) in fake.calls
    assert mail.outbox[-1].subject == "Your SUBRA credits have expired"
    assert "50 credits left on it have expired" in mail.outbox[-1].body


def test_an_outage_never_lapses_anyone(funded, fake, company, settings):
    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    overdue(funded)
    fake.down = True
    assert lifecycle.lapse_overdue(timezone.now(), fake) == 0
    funded.refresh_from_db()
    assert funded.status == SS.PAST_DUE and credits.balance(company) == 50
    assert "Couldn't check overdue subscriptions" in mail.outbox[-1].subject


def test_renewals_paused_stops_lapses_and_expiry(funded, fake, company, settings):
    settings.RENEWALS_PAUSED = True
    overdue(funded)
    lifecycle.run_hourly(provider=fake)
    funded.refresh_from_db()
    assert funded.status == SS.PAST_DUE and credits.balance(company) == 50


def test_a_plan_paid_on_paystack_at_the_last_minute_isnt_lapsed(funded, fake):
    overdue(funded)
    later = (funded.renews_at + timedelta(days=30)).isoformat()
    fake.subs["SUB_team1"] = paystack_sub(code="SUB_team1", status="active", next_date=later)
    assert lifecycle.lapse_overdue(timezone.now(), fake) == 0
    funded.refresh_from_db()
    assert funded.status == SS.ACTIVE


def test_a_cancelled_plan_ends_at_its_period_end(funded, fake, company):
    funded.status, funded.cancelled_at = SS.CANCELLING, timezone.now() - timedelta(days=10)
    funded.renews_at = timezone.now() + timedelta(hours=1)
    funded.save()
    assert lifecycle.end_cancelled_periods(timezone.now()) == 0
    assert credits.balance(company) == 50
    assert lifecycle.end_cancelled_periods(timezone.now() + timedelta(hours=2)) == 1
    funded.refresh_from_db()
    assert funded.status == SS.CANCELLED and credits.balance(company) == 0
    assert "you cancelled it" in mail.outbox[-1].body


def test_a_stale_checkout_is_abandoned_unless_it_was_paid(subs_on, user, company, plan, fake):
    plan.paystack_amount_kes = plan.price_kes
    plan.save()
    subscriptions.start_subscription_checkout(user, plan, "I agree", provider=fake)
    sub = Subscription.objects.get()
    Subscription.objects.filter(pk=sub.pk).update(created_at=timezone.now() - timedelta(hours=25))
    assert lifecycle.abandon_stale_checkouts(timezone.now(), fake) == 1
    sub.refresh_from_db()
    assert sub.status == SS.ABANDONED


def test_a_checkout_paid_at_the_last_minute_activates_instead(subs_on, user, company, plan, fake):
    plan.paystack_amount_kes = plan.price_kes
    plan.save()
    subscriptions.start_subscription_checkout(user, plan, "I agree", provider=fake)
    sub = Subscription.objects.get()
    Subscription.objects.filter(pk=sub.pk).update(created_at=timezone.now() - timedelta(hours=25))
    payment = Payment.objects.get()
    fake.transactions[payment.reference] = transaction(payment.reference)
    assert lifecycle.abandon_stale_checkouts(timezone.now(), fake) == 0
    sub.refresh_from_db()
    assert sub.status == SS.ACTIVE and credits.balance(company) == 115


def test_a_renewal_whose_webhook_was_lost_is_found(funded, fake, company):
    funded.renews_at = timezone.now() - timedelta(hours=7)
    funded.save()
    fake.customer_transactions = [transaction("T_lost", plan_code="PLN_team")]
    fake.subs["SUB_team1"] = paystack_sub(code="SUB_team1", next_date=(timezone.now() + timedelta(days=29)).isoformat())
    assert lifecycle.catch_missed_renewals(timezone.now(), fake) == 1
    assert Payment.objects.get(reference="T_lost").kind == "renewal"
    assert credits.balance(company) == 165
    funded.refresh_from_db()
    assert funded.renews_at > timezone.now()
    assert lifecycle.catch_missed_renewals(timezone.now(), fake) == 0  # nothing due any more


def test_a_renewal_that_never_happened_alerts_once(funded, fake, settings):
    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    funded.renews_at = timezone.now() - timedelta(hours=48, minutes=30)
    funded.save()
    fake.subs["SUB_team1"] = paystack_sub(code="SUB_team1", next_date=funded.renews_at.isoformat())
    lifecycle.catch_missed_renewals(timezone.now(), fake)
    assert sum("Renewal didn't happen" in m.subject for m in mail.outbox) == 1
    lifecycle.catch_missed_renewals(timezone.now() + timedelta(hours=1), fake)
    assert sum("Renewal didn't happen" in m.subject for m in mail.outbox) == 1


def test_the_last_day_of_grace_is_warned_once(funded):
    funded.status, funded.past_due_since = SS.PAST_DUE, timezone.now() - timedelta(days=6, hours=2)
    funded.grace_ends_at = timezone.now() + timedelta(hours=22)
    funded.save()
    assert lifecycle.warn_last_day_of_grace(timezone.now()) == 1
    assert lifecycle.warn_last_day_of_grace(timezone.now()) == 0
    assert mail.outbox[-1].subject == "Last reminder: your SUBRA credits expire tomorrow"


def test_a_reminder_three_days_before_each_charge(funded):
    funded.renews_at = timezone.now() + timedelta(days=2, hours=12)
    funded.save()
    assert lifecycle.send_renewal_reminders(timezone.now()) == 1
    assert lifecycle.send_renewal_reminders(timezone.now()) == 0
    assert mail.outbox[-1].subject.startswith("Your SUBRA plan renews on")
    assert "charge KES 5,000 to your card ending 4081" in mail.outbox[-1].body


def test_daily_checks(funded, company, settings):
    from billing.models import CreditTransaction

    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    old = WebhookDelivery.objects.create(provider="paystack", event_type="x", body={})
    WebhookDelivery.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=91))
    WebhookDelivery.objects.create(provider="paystack", event_type="y", body={})
    CreditTransaction.objects.filter(company=company).update(balance_after=49)
    done = lifecycle.run_daily()
    assert done["deliveries_pruned"] == 1 and WebhookDelivery.objects.count() == 1
    assert done["ledger_mismatches"] == 1 and any("ledger mismatch" in m.subject for m in mail.outbox)
    assert done["plans_out_of_sync"] == 3  # the seeded plans aren't at Paystack yet


def test_billing_tick_runs_offline(funded, fake, capsys):
    call_command("billing_tick", "--now", (timezone.now() + timedelta(days=1)).isoformat(), "--daily")
    out = capsys.readouterr().out
    assert out.startswith("hourly: {'lapsed': 0") and "daily:" in out


def test_the_sweep_waits_a_day_before_giving_up_on_a_card_checkout(subs_on, user, company, plan, fake, monkeypatch):
    from billing import services, tasks

    plan.paystack_amount_kes = plan.price_kes
    plan.save()
    subscriptions.start_subscription_checkout(user, plan, "I agree", provider=fake)
    monkeypatch.setattr(services, "get_provider", lambda name=None: fake)
    payment = Payment.objects.get()
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=2))
    tasks.sweep_pending_payments()
    payment.refresh_from_db()
    assert payment.status == "pending"  # M-Pesa would have given up after 30 minutes
    Payment.objects.filter(pk=payment.pk).update(created_at=timezone.now() - timedelta(hours=25))
    tasks.sweep_pending_payments()
    payment.refresh_from_db()
    assert payment.status == "failed" and payment.customer_message == "We didn't get a confirmation from Paystack."
