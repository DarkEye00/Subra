"""The credits ledger (subscriptions spec, section 5). Every write goes through here.

Rules:
- One append-only stream per company. The balance is the latest row's balance_after.
- Every write locks the Company row first. Lock order everywhere: Company, then Order.
- Every write has an idempotency key, so replaying any event changes nothing the second time.
"""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models import Company, membership_for
from orders.models import Order

from .models import CreditTransaction, Subscription

K = CreditTransaction.Kind


class CreditsError(Exception):
    """Something the customer should see, e.g. not enough credits."""


def lock_company(company):
    """Call inside transaction.atomic(). Returns the locked row."""
    return Company.objects.select_for_update().get(pk=company.pk)


def balance(company):
    last = CreditTransaction.objects.filter(company=company).order_by("-id").values_list("balance_after", flat=True)
    return last.first() or 0


def live_subscription(company):
    return Subscription.objects.filter(company=company, status__in=Subscription.LIVE).select_related("plan").first()


def grant_amount(plan_credits, current_balance):
    """A monthly grant never lifts the balance above CREDIT_CARRYOVER_MULTIPLE months of the plan, and never
    removes anything already in the balance."""
    cap = plan_credits * settings.CREDIT_CARRYOVER_MULTIPLE
    return max(0, min(plan_credits, cap - current_balance))


def _write(company, delta, kind, key, **fields):
    """Append one row. The caller holds the company lock. Returns (row, created)."""
    existing = CreditTransaction.objects.filter(idempotency_key=key).first()
    if existing is not None:
        return existing, False
    row = CreditTransaction.objects.create(company=company, delta=delta, balance_after=balance(company) + delta,
                                           kind=kind, idempotency_key=key, **fields)
    return row, True


def grant_for_payment(payment, plan):
    """The month's credits for a paid subscription or renewal payment (key: the payment)."""
    with transaction.atomic():
        company = lock_company(payment.company)
        amount = grant_amount(plan.monthly_credits, balance(company))
        note = f"{plan.name} plan, {amount} of {plan.monthly_credits} credits"
        if amount < plan.monthly_credits:
            note += f" (balance capped at {plan.carryover_cap})"
        return _write(company, amount, K.GRANT, f"grant:payment:{payment.pk}", payment=payment,
                      subscription=payment.subscription, kes_value=Decimal(payment.amount_kes), note=note)


def topup_for_payment(payment):
    pack = payment.credit_pack
    with transaction.atomic():
        company = lock_company(payment.company)
        return _write(company, pack.credits, K.TOPUP, f"topup:payment:{payment.pk}", payment=payment,
                      kes_value=Decimal(payment.amount_kes), note=f"{pack.name} top-up")


def credit_value_kes(subscription, credits):
    """What the credits spent on a deck are worth, for margin reporting."""
    plan = subscription.plan
    if not plan.monthly_credits:
        return Decimal("0")
    return (Decimal(credits) * Decimal(plan.price_kes) / Decimal(plan.monthly_credits)).quantize(Decimal("0.01"))


def pay_with_credits(order, user):
    """Unlock a deck's download with the company's credits. Raises CreditsError with a customer-facing message."""
    if not settings.SUBSCRIPTIONS_ENABLED:
        raise CreditsError("Paying with credits isn't available.")
    from billing.services import settle_order

    company = membership_for(user).company
    with transaction.atomic():
        company = lock_company(company)
        order = Order.objects.select_for_update().get(pk=order.pk)
        if order.user_id != user.pk:
            raise CreditsError("Only the person who uploaded this deck can pay for it.")
        if not order.can_pay:
            raise CreditsError("This deck can't be paid for right now.")
        if order.company_id != company.pk:
            raise CreditsError("This deck was uploaded before you joined your company, so pay for it with M-Pesa.")
        if not order.credit_cost:
            raise CreditsError("This deck can't be paid for with credits.")
        subscription = live_subscription(company)
        if subscription is None:
            raise CreditsError("Your company has no active plan, so pay for this deck with M-Pesa.")
        have = balance(company)
        if have < order.credit_cost:
            raise CreditsError(f"You have {have} credit{'s' if have != 1 else ''}; this deck needs {order.credit_cost}.")
        _write(company, -order.credit_cost, K.DEBIT, f"debit:order:{order.pk}", order=order, user=user,
               subscription=subscription, kes_value=credit_value_kes(subscription, order.credit_cost),
               note=f"{order.original_name}, by {user.email}")
        settle_order(order, paid_with="credits", note=f"Paid with {order.credit_cost} credits by {user.email}")
    return order


def expire_balance(company, subscription, reason):
    """The subscription ended: whatever is left expires. Skipped if another subscription is live, and while
    RENEWALS_PAUSED is on. Returns the row, or None."""
    if settings.RENEWALS_PAUSED:
        return None
    with transaction.atomic():
        company = lock_company(company)
        if live_subscription(company) is not None:
            return None
        have = balance(company)
        if have <= 0:
            return None
        row, _ = _write(company, -have, K.EXPIRE, f"expire:sub:{subscription.pk}", subscription=subscription,
                        note=reason)
        return row


def clawback_payment(payment):
    """A card payment was refunded or reversed: take back what it granted. The balance may go negative."""
    granted = CreditTransaction.objects.filter(payment=payment, kind__in=[K.GRANT, K.TOPUP]).aggregate(
        t=Sum("delta"))["t"] or 0
    if granted <= 0:
        return None
    with transaction.atomic():
        company = lock_company(payment.company)
        row, created = _write(company, -granted, K.CLAWBACK, f"clawback:payment:{payment.pk}", payment=payment,
                              note=f"Payment {payment.reference or payment.pk} refunded")
    if created and row.balance_after < 0:
        from core.alerts import alert_admins

        transaction.on_commit(lambda: alert_admins(
            f"Credits balance negative for {company.name}",
            f"A refund of payment {payment.pk} clawed back {granted} credits and left {company.name} at "
            f"{row.balance_after}. They can't spend credits until the balance is positive again.",
        ))
    return row


def refund_deck(order, staff_user, note=""):
    """Give back the credits a deck was paid with (from admin). The deck stays downloadable."""
    debit = CreditTransaction.objects.filter(order=order, kind=K.DEBIT).first()
    if debit is None:
        return None
    with transaction.atomic():
        company = lock_company(debit.company)
        row, _ = _write(company, -debit.delta, K.REFUND, f"refund:order:{order.pk}", order=order,
                        created_by=staff_user, kes_value=debit.kes_value,
                        note=note or f"Refund for {order.original_name}")
        return row


def adjust(company, delta, staff_user, note):
    if not note.strip():
        raise ValueError("An adjustment needs a note")
    with transaction.atomic():
        company = lock_company(company)
        row, _ = _write(company, int(delta), K.ADJUST, f"adjust:{uuid.uuid4().hex}", created_by=staff_user,
                        note=note.strip()[:255])
        return row


def usage_by_member(company, since=None):
    """Credits spent per user: [(email, credits, decks)], biggest first."""
    debits = CreditTransaction.objects.filter(company=company, kind=K.DEBIT)
    if since is not None:
        debits = debits.filter(created_at__gte=since)
    refunded = set(CreditTransaction.objects.filter(company=company, kind=K.REFUND).values_list("order_id", flat=True))
    rows = {}
    for d in debits.select_related("user"):
        if d.order_id in refunded:
            continue
        email = d.user.email if d.user_id else "(removed)"
        credits, decks = rows.get(email, (0, 0))
        rows[email] = (credits - d.delta, decks + 1)
    return sorted(((e, c, n) for e, (c, n) in rows.items()), key=lambda r: -r[1])


def ledger_mismatches():
    """Companies whose latest balance_after doesn't equal the sum of their deltas (checked nightly)."""
    out = []
    for company_id in CreditTransaction.objects.values_list("company_id", flat=True).distinct():
        rows = CreditTransaction.objects.filter(company_id=company_id)
        total = rows.aggregate(t=Sum("delta"))["t"] or 0
        latest = rows.order_by("-id").values_list("balance_after", flat=True).first()
        if total != latest:
            out.append((company_id, total, latest))
    return out


def period_start(subscription, now=None):
    """Start of the current billing period, for "credits used this period"."""
    now = now or timezone.now()
    if subscription and subscription.renews_at:
        return subscription.renews_at - timedelta(days=30)
    return now - timedelta(days=30)
