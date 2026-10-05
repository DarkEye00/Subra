from decimal import Decimal

import pytest
from django.core import mail

from billing import services
from billing.models import Payment
from billing.providers import PaymentEvent
from orders.models import Order


@pytest.fixture
def limit(settings):
    settings.PAYSTACK_STARTER_LIMIT_KES = 1000
    return 1000


def collect(order_factory, amount, ref, provider="paystack"):
    order = order_factory(f"{ref}.pptx", status=Order.Status.READY, price_kes=amount)
    Payment.objects.create(order=order, provider=provider, provider_ref=ref, phone="254712345678", amount_kes=amount)
    services.apply_event(provider, PaymentEvent(provider_ref=ref, state="paid", amount=Decimal(amount),
                                                fee=Decimal("1"), raw={}), source="callback")


def limit_alerts():
    return [m.subject for m in mail.outbox if "Starter limit" in m.subject]


def test_alerts_once_at_80_and_95_percent(order_factory, limit):
    collect(order_factory, 700, "A")
    assert limit_alerts() == []
    collect(order_factory, 150, "B")  # 850 of 1000
    assert limit_alerts() == ["[SUBRA] Paystack Starter limit 80% used"]
    collect(order_factory, 50, "C")  # 900
    collect(order_factory, 60, "D")  # 960
    assert limit_alerts() == ["[SUBRA] Paystack Starter limit 80% used", "[SUBRA] Paystack Starter limit 95% used"]


def test_one_payment_can_cross_both_thresholds(order_factory, limit):
    collect(order_factory, 990, "A")
    assert len(limit_alerts()) == 2


def test_refunds_dont_reset_the_count(order_factory, limit):
    collect(order_factory, 850, "A")
    Payment.objects.filter(provider_ref="A").update(status="refunded")
    collect(order_factory, 10, "B")
    assert limit_alerts() == ["[SUBRA] Paystack Starter limit 80% used"]


def test_no_alerts_once_the_business_is_registered(order_factory, settings):
    settings.PAYSTACK_STARTER_LIMIT_KES = 0
    collect(order_factory, 990, "A")
    assert limit_alerts() == []


def test_other_providers_dont_count(order_factory, limit):
    collect(order_factory, 990, "X", provider="intasend")
    assert limit_alerts() == []
