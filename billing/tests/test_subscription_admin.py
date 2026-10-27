import pytest
from django.urls import reverse

from accounts.models import Membership, User
from billing import credits
from billing.models import CreditTransaction


@pytest.fixture
def staff(client, db):
    admin = User.objects.create_superuser("joe@example.co.ke", "a-strong-password-1")
    client.force_login(admin)
    return client


@pytest.mark.parametrize("model", ["billing_plan", "billing_creditpack", "billing_subscription",
                                   "billing_credittransaction", "billing_webhookdelivery", "billing_payment",
                                   "billing_pricetier", "accounts_company"])
def test_changelists_load(staff, model, subscribed, fund):
    fund(5)
    assert staff.get(reverse(f"admin:{model}_changelist")).status_code == 200


def test_subscription_and_company_pages_load(staff, subscribed, member):
    assert staff.get(reverse("admin:billing_subscription_change", args=[subscribed.pk])).status_code == 200
    assert staff.get(reverse("admin:accounts_company_change", args=[subscribed.company.pk])).status_code == 200


def test_staff_adjust_credits_with_a_note(staff, company):
    url = reverse("admin:accounts_company_adjust", args=[company.pk])
    assert staff.get(url).status_code == 200
    resp = staff.post(url, {"delta": "12", "note": "Goodwill for the outage"})
    assert resp.status_code == 302
    row = CreditTransaction.objects.get()
    assert (row.delta, row.note, row.created_by.email) == (12, "Goodwill for the outage", "joe@example.co.ke")
    assert credits.balance(company) == 12


def test_handing_over_ownership_moves_the_roles(staff, company, member, user):
    url = reverse("admin:accounts_company_change", args=[company.pk])
    data = {"name": company.name, "owner": member.pk, "archived_at": "",
            "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
            "invites-TOTAL_FORMS": "0", "invites-INITIAL_FORMS": "0"}
    resp = staff.post(url, data)
    assert resp.status_code == 302, resp.content.decode()[:2000]
    company.refresh_from_db()
    assert company.owner == member
    assert Membership.objects.get(user=member).is_owner and not Membership.objects.get(user=user).is_owner


def test_only_a_member_can_become_owner(staff, company):
    stranger = User.objects.create_user("stranger@example.co.ke", "a-strong-password-1")
    url = reverse("admin:accounts_company_change", args=[company.pk])
    data = {"name": company.name, "owner": stranger.pk, "archived_at": "",
            "memberships-TOTAL_FORMS": "0", "memberships-INITIAL_FORMS": "0",
            "invites-TOTAL_FORMS": "0", "invites-INITIAL_FORMS": "0"}
    resp = staff.post(url, data)
    assert resp.status_code == 200 and "must already be a member" in resp.content.decode()
