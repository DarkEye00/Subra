import pytest


@pytest.fixture
def user(db):
    from accounts.models import User

    return User.objects.create_user("wanjiku@example.co.ke", "a-strong-password-1")


@pytest.fixture
def theme(db):
    from orders.models import Theme

    return Theme.objects.get(slug="navy")


@pytest.fixture
def quoted_order(db, user, theme):
    from billing.models import PriceTier
    from orders.models import Order

    tier = PriceTier.objects.get(name="Standard")
    order = Order.objects.create(user=user, original_name="Q3 review.pptx", slide_count=20, tier=tier,
                                 price_kes=tier.price_kes, theme=theme)
    order.transition(Order.Status.QUOTED)
    return order
