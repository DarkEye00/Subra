"""The credits ledger and paying for decks with credits (subscriptions spec, sections 5 and 6)."""

import pytest
from django.db import IntegrityError

from accounts.models import Company, Membership, company_for, membership_for
from billing import credits
from billing.models import CreditTransaction, Payment, Subscription
from orders.models import Order

S = Order.Status
K = CreditTransaction.Kind


def ready_deck(order_factory, owner, cost=7):
    order = order_factory(status=S.READY, owner=owner, credit_cost=cost)
    order.company = company_for(owner)
    order.save()
    return order


# --- companies ---------------------------------------------------------------

def test_a_personal_company_is_created_on_first_use(user):
    assert not Company.objects.exists()
    membership = membership_for(user)
    assert membership.is_owner and membership.company.owner == user and membership.company.name == user.email
    assert membership_for(user) == membership  # once only
    assert Company.objects.count() == 1


def test_one_company_per_person(user, company):
    other = Company.objects.create(name="Acme", owner=user)
    with pytest.raises(IntegrityError):
        Membership.objects.create(company=other, user=user)


# --- the cap ---------------------------------------------------------------

@pytest.mark.parametrize("balance, granted", [(0, 115), (100, 115), (115, 115), (200, 30), (230, 0), (400, 0)])
def test_a_grant_never_lifts_the_balance_above_two_months(balance, granted):
    assert credits.grant_amount(115, balance) == granted


def test_the_carryover_multiple_is_a_setting(settings):
    settings.CREDIT_CARRYOVER_MULTIPLE = 3
    assert credits.grant_amount(115, 300) == 45


# --- the ledger ------------------------------------------------------------

def card_payment(company, subscription, kind=Payment.Kind.RENEWAL, amount=5000, ref="T1", pack=None):
    return Payment.objects.create(kind=kind, channel=Payment.Channel.CARD, company=company, subscription=subscription,
                                  credit_pack=pack, provider="paystack", provider_ref=ref, reference=ref,
                                  amount_kes=amount, status=Payment.Status.PAID)


def test_a_grant_is_written_once_per_payment(company, subscribed):
    payment = card_payment(company, subscribed)
    row, created = credits.grant_for_payment(payment, subscribed.plan)
    assert created and row.delta == 115 and row.balance_after == 115 and row.kind == K.GRANT
    again, created = credits.grant_for_payment(payment, subscribed.plan)
    assert not created and again == row
    assert credits.balance(company) == 115


def test_a_capped_grant_says_so(company, subscribed, fund):
    fund(200)
    row, _ = credits.grant_for_payment(card_payment(company, subscribed), subscribed.plan)
    assert row.delta == 30 and row.balance_after == 230
    assert "capped at 230" in row.note


def test_top_ups_are_never_destroyed_by_the_cap(company, subscribed, fund):
    from billing.models import CreditPack

    fund(220)
    pack = CreditPack.objects.get(credits=25)
    credits.topup_for_payment(card_payment(company, subscribed, kind=Payment.Kind.TOPUP, amount=1150, ref="T2",
                                           pack=pack))
    assert credits.balance(company) == 245  # above the cap: a top-up isn't clipped
    row, _ = credits.grant_for_payment(card_payment(company, subscribed, ref="T3"), subscribed.plan)
    assert row.delta == 0 and credits.balance(company) == 245


def test_expiry_takes_the_whole_balance_once(company, subscribed, fund):
    fund(40)
    subscribed.status = Subscription.Status.CANCELLED
    subscribed.save()
    row = credits.expire_balance(company, subscribed, "Plan cancelled")
    assert row.delta == -40 and credits.balance(company) == 0
    assert credits.expire_balance(company, subscribed, "Plan cancelled") is None


def test_no_expiry_while_another_subscription_is_live(company, subscribed, plan, fund):
    fund(40)
    old = Subscription.objects.create(company=company, plan=plan, status=Subscription.Status.CANCELLED,
                                      email=company.owner.email)
    assert credits.expire_balance(company, old, "replaced") is None
    assert credits.balance(company) == 40


def test_no_expiry_while_renewals_are_paused(company, subscribed, fund, settings):
    settings.RENEWALS_PAUSED = True
    fund(40)
    subscribed.status = Subscription.Status.CANCELLED
    subscribed.save()
    assert credits.expire_balance(company, subscribed, "x") is None
    assert credits.balance(company) == 40


def test_clawback_takes_back_what_a_payment_granted_and_alerts_when_negative(company, subscribed, settings,
                                                                           django_capture_on_commit_callbacks):
    from django.core import mail

    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    payment = card_payment(company, subscribed)
    credits.grant_for_payment(payment, subscribed.plan)
    CreditTransaction.objects.create(company=company, delta=-100, balance_after=15, kind=K.DEBIT,
                                     idempotency_key="debit:test")
    with django_capture_on_commit_callbacks(execute=True):
        row = credits.clawback_payment(payment)
        assert credits.clawback_payment(payment) == row  # once
    assert row.delta == -115 and credits.balance(company) == -100
    assert "negative" in mail.outbox[0].subject


def test_adjustments_need_a_note(company):
    with pytest.raises(ValueError):
        credits.adjust(company, 5, None, "  ")


def test_the_ledger_integrity_check(company, fund):
    fund(10)
    assert credits.ledger_mismatches() == []
    CreditTransaction.objects.filter(company=company).update(balance_after=9)
    assert credits.ledger_mismatches() == [(company.pk, 10, 9)]


# --- paying for a deck with credits ------------------------------------------

def test_paying_with_credits_unlocks_the_download_and_debits_once(subs_on, user, subscribed, fund, order_factory):
    fund(20)
    order = ready_deck(order_factory, user)
    credits.pay_with_credits(order, user)
    order.refresh_from_db()
    assert order.is_paid and order.paid_with == "credits"
    assert order.expires_at > order.paid_at
    assert credits.balance(subscribed.company) == 13
    assert order.events.last().note == f"Paid with 7 credits by {user.email}"
    assert order.credits_paid_note == f"Paid with 7 credits by {user.email}"
    debit = CreditTransaction.objects.get(kind=K.DEBIT)
    assert (debit.user, debit.order, str(debit.kes_value)) == (user, order, "304.35")  # 7 × 5,000 / 115
    with pytest.raises(credits.CreditsError, match="can't be paid"):
        credits.pay_with_credits(order, user)
    assert credits.balance(subscribed.company) == 13


def test_not_enough_credits(subs_on, user, subscribed, fund, order_factory):
    fund(3)
    order = ready_deck(order_factory, user)
    with pytest.raises(credits.CreditsError, match="You have 3 credits; this deck needs 7."):
        credits.pay_with_credits(order, user)
    order.refresh_from_db()
    assert not order.is_paid and order.can_pay


def test_credits_need_a_live_subscription(subs_on, user, company, fund, order_factory):
    fund(20)
    order = ready_deck(order_factory, user)
    with pytest.raises(credits.CreditsError, match="no active plan"):
        credits.pay_with_credits(order, user)


def test_credits_are_off_with_the_flag(user, subscribed, fund, order_factory):
    fund(20)
    with pytest.raises(credits.CreditsError):
        credits.pay_with_credits(ready_deck(order_factory, user), user)


def test_a_negative_balance_blocks_spending(subs_on, user, subscribed, fund, order_factory):
    fund(-5)
    with pytest.raises(credits.CreditsError, match="You have -5 credits"):
        credits.pay_with_credits(ready_deck(order_factory, user), user)


def test_a_deck_from_a_previous_company_is_paid_by_mpesa(subs_on, user, subscribed, fund, order_factory):
    fund(20)
    order = ready_deck(order_factory, user)
    order.company = Company.objects.create(name="Old", owner=user)
    order.save()
    with pytest.raises(credits.CreditsError, match="before you joined"):
        credits.pay_with_credits(order, user)


def test_two_members_and_seven_credits(subs_on, user, member, subscribed, fund, order_factory):
    """Two 4-credit decks and 7 credits: the first succeeds, the second is told why and can use M-Pesa."""
    fund(7)
    mine, theirs = ready_deck(order_factory, user, cost=4), ready_deck(order_factory, member, cost=4)
    credits.pay_with_credits(mine, user)
    with pytest.raises(credits.CreditsError, match="You have 3 credits; this deck needs 4."):
        credits.pay_with_credits(theirs, member)
    theirs.refresh_from_db()
    assert theirs.can_pay and credits.balance(subscribed.company) == 3


def test_a_member_cant_pay_for_someone_elses_deck(subs_on, user, member, subscribed, fund, order_factory):
    fund(20)
    with pytest.raises(credits.CreditsError, match="Only the person"):
        credits.pay_with_credits(ready_deck(order_factory, user), member)


def test_refunding_a_credit_paid_deck_returns_the_credits(subs_on, user, subscribed, fund, order_factory):
    fund(20)
    order = ready_deck(order_factory, user)
    credits.pay_with_credits(order, user)
    credits.refund_deck(order, None)
    credits.refund_deck(order, None)
    assert credits.balance(subscribed.company) == 20
    order.refresh_from_db()
    assert order.is_paid  # the deck stays downloadable, like mark_refunded for M-Pesa
    assert credits.usage_by_member(subscribed.company) == []


def test_usage_by_member(subs_on, user, member, subscribed, fund, order_factory):
    fund(30)
    credits.pay_with_credits(ready_deck(order_factory, user), user)
    credits.pay_with_credits(ready_deck(order_factory, member, cost=10), member)
    credits.pay_with_credits(ready_deck(order_factory, member, cost=4), member)
    assert credits.usage_by_member(subscribed.company) == [(member.email, 14, 2), (user.email, 7, 1)]
