"""The order pages for subscribers: paying with credits and who may see which deck (subscriptions spec, section 6)."""

import pytest
from django.urls import reverse

from accounts.models import User
from billing import credits

URLS = ["detail", "download", "status", "polish", "theme", "pay", "pay_credits", "discard"]
POSTS = {"polish", "theme", "pay", "pay_credits", "discard"}


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


def page(client, order):
    return client.get(reverse("orders:detail", args=[order.public_id])).content.decode()


def test_a_subscriber_pays_with_credits_and_downloads(subs_on, logged_in, ready_order, subscribed, fund):
    fund(10)
    html = page(logged_in, ready_order)
    assert "Use 4 credits to download" in html and "You have 10 credits." in html
    assert "Or pay KES 200 with M-Pesa" in html and "Pay KES 200 to download" in html
    resp = logged_in.post(reverse("orders:pay_credits", args=[ready_order.public_id]))
    assert resp.status_code == 302
    ready_order.refresh_from_db()
    assert ready_order.is_paid and credits.balance(subscribed.company) == 6
    html = page(logged_in, ready_order)
    assert "Paid with 4 credits by wanjiku@example.co.ke" in html and "Download the .pptx" in html
    assert logged_in.get(reverse("orders:download", args=[ready_order.public_id])).status_code == 302
    assert "Paid, credits" in logged_in.get(reverse("orders:list")).content.decode()


def test_not_enough_credits_points_the_owner_to_a_top_up(subs_on, logged_in, ready_order, subscribed, fund):
    fund(3)
    html = page(logged_in, ready_order)
    assert "You have 3 credits; this deck needs 4." in html and "Buy a top-up" in html
    assert "Use 4 credits" not in html and "Pay KES 200 to download" in html
    logged_in.post(reverse("orders:pay_credits", args=[ready_order.public_id]))
    ready_order.refresh_from_db()
    assert not ready_order.is_paid


def test_a_past_due_plan_says_until_when_credits_work(subs_on, logged_in, ready_order, subscribed, fund):
    from datetime import timedelta

    from django.utils import timezone

    fund(10)
    subscribed.status = "past_due"
    subscribed.grace_ends_at = timezone.now() + timedelta(days=5)
    subscribed.save()
    assert "card payment failed. Credits work until" in page(logged_in, ready_order)


def test_without_a_plan_the_page_is_unchanged(subs_on, logged_in, ready_order):
    html = page(logged_in, ready_order)
    assert "credits" not in html.lower()
    assert "Your redesigned deck is ready" in html and "Pay KES 200 to download" in html


def test_with_the_flag_off_credits_are_hidden_and_the_endpoint_is_gone(logged_in, ready_order, subscribed, fund):
    fund(10)
    assert "credits" not in page(logged_in, ready_order).lower()
    assert logged_in.post(reverse("orders:pay_credits", args=[ready_order.public_id])).status_code == 404


def test_a_quote_shows_the_credit_price(subs_on, logged_in, order_factory, user, subscribed):
    from accounts.models import company_for

    order = order_factory(credit_cost=7, company=company_for(user))
    assert "or 7 credits" in page(logged_in, order)


# --- who may see what -------------------------------------------------------

def _status(client, name, order):
    url = reverse(f"orders:{name}", args=[order.public_id])
    return (client.post(url) if name in POSTS else client.get(url)).status_code


@pytest.fixture
def members_paid_deck(subs_on, member, subscribed, fund, order_factory, tmp_path):
    """A paid, ready deck uploaded by a member, with a real result file."""
    from django.core.files.base import ContentFile

    from accounts.models import company_for

    fund(10)
    order = order_factory(status="ready", owner=member, credit_cost=4, company=company_for(member))
    order.result_file.save("polished.pptx", ContentFile(b"pptx"), save=True)
    credits.pay_with_credits(order, member)
    order.refresh_from_db()
    return order


def test_the_owner_can_see_and_download_a_members_deck_but_not_change_it(client, user, members_paid_deck):
    client.force_login(user)
    seen = {name: _status(client, name, members_paid_deck) for name in URLS}
    assert seen["detail"] == 200 and seen["download"] == 302 and seen["status"] == 204  # poll target: "reload"
    for name in POSTS:
        assert seen[name] == 404, name
    html = page(client, members_paid_deck)
    assert "uploaded by otieno@example.co.ke" in html and "Paid with 4 credits by otieno@example.co.ke" in html
    assert "Discard" not in html and "Try another theme" not in html


def test_the_owner_sees_an_unpaid_member_deck_without_pay_forms(client, user, member, subs_on, subscribed,
                                                                order_factory):
    from accounts.models import company_for

    order = order_factory(status="ready", owner=member, credit_cost=4, company=company_for(member))
    client.force_login(user)
    html = page(client, order)
    assert "Not paid for yet" in html and "M-Pesa number" not in html and "Use 4 credits" not in html


def test_members_cant_see_each_others_decks(client, member, members_paid_deck, company):
    from accounts.models import Membership

    other = User.objects.create_user("akinyi@example.co.ke", "a-strong-password-1")
    Membership.objects.create(company=company, user=other)
    client.force_login(other)
    for name in URLS:
        assert _status(client, name, members_paid_deck) == 404, name


def test_strangers_see_nothing(client, members_paid_deck):
    client.force_login(User.objects.create_user("stranger@example.co.ke", "a-strong-password-1"))
    for name in URLS:
        assert _status(client, name, members_paid_deck) == 404, name


def test_a_removed_owner_role_loses_access(client, user, members_paid_deck, company):
    other = User.objects.create_user("new-owner@example.co.ke", "a-strong-password-1")
    company.owner = other
    company.save()
    client.force_login(user)
    assert _status(client, "detail", members_paid_deck) == 404
