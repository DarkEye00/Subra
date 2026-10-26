"""Company accounts and the plan pages (subscriptions spec, sections 2 and 8)."""

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from accounts import companies
from accounts.models import Company, Invite, Membership, User, company_for, membership_for
from billing import credits


@pytest.fixture
def owner_client(client, user):
    client.force_login(user)
    return client


def invite_token(email="otieno@example.co.ke"):
    body = mail.outbox[-1].body
    assert email in mail.outbox[-1].to
    return re.search(r"/accounts/invites/([^/\s]+)/", body).group(1)


# --- invites --------------------------------------------------------------------------------------------------

def test_an_invite_is_emailed_and_only_its_hash_is_stored(subs_on, user, company, on_commit, settings):
    settings.SITE_URL = "https://subra.co.ke"
    invite, token = companies.create_invite(user, " Otieno@Example.co.ke ")
    assert invite.email == "otieno@example.co.ke" and invite.token_hash != token and len(invite.token_hash) == 64
    assert mail.outbox[-1].subject == f"{user.email} invited you to {company.name} on SUBRA"
    assert f"https://subra.co.ke/accounts/invites/{token}/" in mail.outbox[-1].body
    assert companies.find_invite(token) == invite
    assert companies.find_invite("nope") is None


def test_inviting_again_replaces_the_old_invite(subs_on, user, company, on_commit):
    _, first = companies.create_invite(user, "otieno@example.co.ke")
    _, second = companies.create_invite(user, "otieno@example.co.ke")
    assert companies.find_invite(first) is None and companies.find_invite(second) is not None


def test_only_the_owner_invites(subs_on, member, on_commit):
    with pytest.raises(companies.CompanyError, match="owner"):
        companies.create_invite(member, "akinyi@example.co.ke")


def test_members_cant_be_invited_twice(subs_on, user, member):
    with pytest.raises(companies.CompanyError, match="already a member"):
        companies.create_invite(user, member.email)


def test_expired_and_revoked_invites_dont_work(subs_on, user, on_commit):
    invite, token = companies.create_invite(user, "otieno@example.co.ke")
    Invite.objects.filter(pk=invite.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert companies.find_invite(token) is None
    invite, token = companies.create_invite(user, "otieno@example.co.ke")
    companies.revoke_invite(user, invite.pk)
    assert companies.find_invite(token) is None


def test_accepting_with_a_new_account_locks_the_email(subs_on, client, user, company, on_commit):
    companies.create_invite(user, "otieno@example.co.ke")
    token = invite_token()
    url = reverse("accounts:invite_accept", args=[token])
    html = client.get(url).content.decode()
    assert f"Join {company.name}" in html and "otieno@example.co.ke" in html
    resp = client.post(url, {"email": "attacker@example.co.ke", "password1": "a-strong-password-1",
                             "password2": "a-strong-password-1"})
    assert resp.status_code == 302
    joined = User.objects.get(email="otieno@example.co.ke")  # the token decides, not the typed email
    assert not User.objects.filter(email="attacker@example.co.ke").exists()
    assert membership_for(joined).company == company and not membership_for(joined).is_owner
    assert client.get(url).status_code == 404  # used


def test_accepting_with_an_existing_account(subs_on, client, user, company, on_commit):
    other = User.objects.create_user("otieno@example.co.ke", "a-strong-password-1")
    old = company_for(other)
    companies.create_invite(user, other.email)
    url = reverse("accounts:invite_accept", args=[invite_token()])
    assert "Sign in as otieno@example.co.ke" in client.get(url).content.decode()
    client.force_login(other)
    client.post(url)
    assert membership_for(other).company == company
    old.refresh_from_db()
    assert old.archived_at is not None


def test_the_signed_in_user_must_be_the_invited_one(subs_on, client, user, on_commit):
    companies.create_invite(user, "otieno@example.co.ke")
    url = reverse("accounts:invite_accept", args=[invite_token()])
    client.force_login(User.objects.create_user("akinyi@example.co.ke", "a-strong-password-1"))
    assert "This invitation is for otieno@example.co.ke" in client.get(url).content.decode()
    client.post(url)
    assert not Membership.objects.filter(user__email="akinyi@example.co.ke", company=company_for(user)).exists()


@pytest.mark.parametrize("blocker, message", [
    ("plan", "Cancel your own plan first"), ("credits", "still has credits"),
    ("members", "has other members"), ("unpaid", "Pay for or discard your unpaid deck"),
])
def test_joining_is_refused_while_something_would_be_stranded(subs_on, user, plan, order_factory, blocker, message,
                                                             on_commit):
    from billing.models import Subscription

    other = User.objects.create_user("otieno@example.co.ke", "a-strong-password-1")
    own = company_for(other)
    if blocker == "plan":
        Subscription.objects.create(company=own, plan=plan, status="active", email=other.email)
    elif blocker == "credits":
        credits.adjust(own, 5, None, "test")
    elif blocker == "members":
        Membership.objects.create(company=own, user=User.objects.create_user("x@example.co.ke", "pw-strong-12345"))
    else:
        order_factory(status="ready", owner=other)
    invite, _ = companies.create_invite(user, other.email)
    assert message in companies.why_cant_join(other, invite)
    with pytest.raises(companies.CompanyError):
        companies.accept_invite(other, invite)
    assert membership_for(other).company == own


def test_removing_a_member_keeps_their_login_and_decks(subs_on, user, member, company, order_factory):
    order = order_factory(owner=member, company=company)
    companies.remove_member(user, member.pk)
    assert not Membership.objects.filter(user=member, company=company).exists()
    fresh = membership_for(member)
    assert fresh.company != company and fresh.is_owner
    order.refresh_from_db()
    assert order.company == company  # the owner can still download what the company paid for
    with pytest.raises(companies.CompanyError, match="can't remove yourself"):
        companies.remove_member(user, user.pk)


def test_invites_need_the_flag(user, company):
    with pytest.raises(companies.CompanyError):
        companies.create_invite(user, "otieno@example.co.ke")


# --- the plan pages ---------------------------------------------------------------------------------------

def test_the_plans_page_shows_price_per_credit_and_decks(subs_on, client):
    html = client.get(reverse("accounts:plans")).content.decode()
    assert "KES 2,000" in html and "40 credits a month" in html and "About KES 50.0 a credit" in html
    assert "Up to 10 small decks" in html and "Up to 4 large decks" in html
    assert "M-Pesa can't be charged monthly" in html


def test_plan_pages_are_hidden_with_the_flag_off(client, user):
    client.force_login(user)
    for name in ["plans", "plan"]:
        assert client.get(reverse(f"accounts:{name}")).status_code == 404


def test_subscribing_needs_the_consent_box(subs_on, owner_client, plan):
    plan.paystack_amount_kes = plan.price_kes
    plan.save()
    resp = owner_client.post(reverse("accounts:subscribe"), {"plan": "team"}, follow=True)
    assert "Tick the box" in resp.content.decode()


def test_subscribing_redirects_to_paystack(subs_on, owner_client, plan, monkeypatch):
    from billing import subscriptions
    from billing.tests.test_subscriptions import FakePaystack

    fake = FakePaystack()
    monkeypatch.setattr(subscriptions, "paystack", lambda: fake)
    plan.paystack_amount_kes = plan.price_kes
    plan.save()
    resp = owner_client.post(reverse("accounts:subscribe"), {"plan": "team", "consent": "1"})
    assert resp.status_code == 302 and resp["Location"].startswith("https://checkout.paystack.com/subra-sub-")
    from billing.models import Subscription

    assert Subscription.objects.get().consent_text == (
        "I agree that SUBRA charges KES 5,000 to my card every month for the Team plan until I cancel.")


def test_the_owners_plan_page(subs_on, owner_client, subscribed, member, fund, order_factory, user):
    fund(30)
    order = order_factory(status="ready", owner=member, credit_cost=7, company=subscribed.company)
    credits.pay_with_credits(order, member)
    html = owner_client.get(reverse("accounts:plan")).content.decode()
    assert "Team plan: 115 credits a month for KES 5,000" in html and "card ending 4081" in html
    assert 'data-balance>23<' in html
    assert member.email in html and "7 credits this month" in html
    assert "Q3 review.pptx" in html and "paid with 7 credits" in html
    assert "Top up" in html and "25 credits for KES 1,150" in html
    assert "Switch to Solo" in html and "Cancel plan" in html
    assert "Deck download" in html  # the ledger history
    csv = owner_client.get(reverse("accounts:usage_csv")).content.decode()
    assert "Deck download,-7,23,otieno@example.co.ke,Q3 review.pptx" in csv


def test_a_members_plan_page(subs_on, client, member, subscribed, fund):
    fund(30)
    client.force_login(member)
    html = client.get(reverse("accounts:plan")).content.decode()
    assert 'data-balance>30<' in html and "Your usage this month" in html
    assert "Top up" not in html and "Invite" not in html and "Cancel plan" not in html
    assert client.get(reverse("accounts:usage_csv")).status_code == 404


def test_the_past_due_banner(subs_on, owner_client, client, subscribed, member):
    from billing import subscriptions

    subscriptions.mark_past_due(subscribed)
    html = owner_client.get(reverse("accounts:plan")).content.decode()
    assert "We couldn't charge your card ending 4081" in html and "Pay now with a new card" in html
    client.force_login(member)
    assert "Ask wanjiku@example.co.ke to update the company card" in client.get(reverse("accounts:plan")).content.decode()


def test_the_nav_shows_the_balance_once_a_company_has_had_a_plan(subs_on, owner_client, company, subscribed, fund):
    fund(12)
    html = owner_client.get(reverse("orders:list")).content.decode()
    assert "Plan &amp; credits" in html and 'aria-label="12 credits"' in html


def test_the_nav_stays_plain_without_a_plan(subs_on, owner_client, company):
    assert "Plan &amp; credits" not in owner_client.get(reverse("orders:list")).content.decode()


def test_invite_and_remove_from_the_page(subs_on, owner_client, member, on_commit):
    owner_client.post(reverse("accounts:invite"), {"email": "akinyi@example.co.ke"})
    assert Invite.objects.filter(email="akinyi@example.co.ke").exists()
    owner_client.post(reverse("accounts:remove_member", args=[member.pk]))
    assert not Membership.objects.filter(user=member, company__owner__email="wanjiku@example.co.ke").exists()


def test_simulated_subscription_in_debug(subs_on, owner_client, company, settings):
    settings.DEBUG = True
    settings.PAYSTACK_SECRET_KEY = ""
    owner_client.post(reverse("accounts:dev_subscribe"), {"plan": "solo"})
    assert credits.balance(company) == 40
    owner_client.post(reverse("accounts:dev_subscribe"), {"what": "renew"})
    assert credits.balance(company) == 80
    owner_client.post(reverse("accounts:dev_subscribe"), {"what": "fail"})
    assert credits.live_subscription(company).status == "past_due"


def test_simulation_is_off_outside_debug(subs_on, owner_client):
    assert owner_client.post(reverse("accounts:dev_subscribe"), {"plan": "solo"}).status_code == 404


# --- password reset ---------------------------------------------------------------------------------------

def test_password_reset_by_email(client, user):
    html = client.get(reverse("login")).content.decode()
    assert "Forgot your password?" in html
    client.post(reverse("accounts:password_reset"), {"email": user.email})
    assert mail.outbox[-1].subject == "Reset your SUBRA password"
    link = re.search(r"https?://[^/]+(/accounts/password/reset/\S+)", mail.outbox[-1].body).group(1)
    resp = client.get(link, follow=True)
    form_url = resp.redirect_chain[-1][0] if resp.redirect_chain else link
    resp = client.post(form_url, {"new_password1": "another-strong-pw-9", "new_password2": "another-strong-pw-9"})
    assert resp.status_code == 302
    user.refresh_from_db()
    assert user.check_password("another-strong-pw-9")


def test_companies_are_created_lazily(user):
    assert not Company.objects.exists()
    company_for(user)
    assert Company.objects.count() == 1


def test_legal_pages_describe_plans_only_when_they_exist(client, db, settings):
    assert "Plans and credits" not in client.get(reverse("terms")).content.decode()
    settings.SUBSCRIPTIONS_ENABLED = True
    terms = client.get(reverse("terms")).content.decode()
    assert "Plans and credits" in terms and "including top-ups" in terms and "7 days" in terms
    privacy = client.get(reverse("privacy")).content.decode()
    assert "last four digits" in privacy and "owner can see and download the decks" in privacy
