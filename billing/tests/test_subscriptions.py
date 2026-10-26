"""Subscriptions billed through Paystack Plans (subscriptions spec, revision 2).

Paystack is faked below its HTTP layer: FakePaystack is the real PaystackProvider with _request routed to canned
responses built from the fields in Paystack's API reference. These are NOT recorded payloads (paystack.com wasn't
reachable from the build environment); record real ones in test mode before launch (spec, section 11).
"""

import hashlib
import hmac
import json
from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone

from billing import credits, subscriptions
from billing.models import CreditPack, Payment, Plan, Subscription, WebhookDelivery
from billing.providers import ProviderError
from billing.providers.paystack import PaystackProvider

SS = Subscription.Status
SECRET = "sk_test_subra"


class FakePaystack(PaystackProvider):
    def __init__(self):
        super().__init__(secret_key=SECRET)
        self.calls = []
        self.transactions = {}  # reference -> transaction data
        self.subs = {}  # code -> subscription data
        self.customer_subs = []  # what GET /subscription?customer= lists
        self.customer_transactions = []
        self.down = False
        self.on_init = None

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.down:
            raise ProviderError("Paystack unreachable: down")
        if path == "/transaction/initialize":
            if self.on_init:
                self.on_init(body)
            return {"status": True, "data": {"authorization_url": f"https://checkout.paystack.com/{body['reference']}",
                                             "access_code": "ac", "reference": body["reference"]}}
        if path.startswith("/transaction/verify/"):
            ref = path.rsplit("/", 1)[1]
            return {"status": True, "data": self.transactions.get(ref, {"reference": ref, "status": "abandoned"})}
        if path.startswith("/transaction?"):
            return {"status": True, "data": self.customer_transactions}
        if path.startswith("/subscription?"):
            return {"status": True, "data": self.customer_subs}
        if path.endswith("/manage/link"):
            return {"status": True, "data": {"link": "https://paystack.com/manage/subscriptions/x"}}
        if path.startswith("/subscription/") and method == "GET":
            return {"status": True, "data": self.subs[path.rsplit("/", 1)[1]]}
        if path in ("/subscription/disable", "/subscription/enable"):
            return {"status": True, "message": "ok"}
        if path == "/plan":
            return {"status": True, "data": {"plan_code": f"PLN_{body['name'].split()[-1].lower()}"}}
        if path.startswith("/plan/"):
            return {"status": True, "data": {}}
        raise AssertionError(f"unexpected Paystack call {method} {path}")

    def paths(self):
        return [p for _, p, _ in self.calls]


def transaction(reference, amount_kes=5000, plan_code="PLN_team", channel="card", status="success"):
    return {
        "id": 900, "status": status, "reference": reference, "amount": amount_kes * 100, "currency": "KES",
        "gateway_response": "Approved" if status == "success" else "Declined", "channel": channel,
        "fees": int(amount_kes * 2.9), "paid_at": "2026-10-27T09:00:00.000Z",
        "customer": {"id": 101, "customer_code": "CUS_1", "email": "wanjiku@example.co.ke"},
        "authorization": {"authorization_code": "AUTH_1", "last4": "4081", "exp_month": "12", "exp_year": "2030",
                          "brand": "visa", "reusable": True},
        "plan": {"plan_code": plan_code, "name": "SUBRA Team"} if plan_code else {},
    }


def paystack_sub(code="SUB_1", status="active", next_date="2026-11-27T09:00:00.000Z", plan_code="PLN_team"):
    return {"subscription_code": code, "email_token": "tok_1", "status": status, "next_payment_date": next_date,
            "plan": {"plan_code": plan_code}, "customer": {"id": 101, "customer_code": "CUS_1"},
            "authorization": {"last4": "4081", "brand": "visa", "exp_month": "12", "exp_year": "2030"}}


@pytest.fixture
def fake(monkeypatch):
    fake = FakePaystack()
    monkeypatch.setattr(subscriptions, "paystack", lambda: fake)
    return fake


@pytest.fixture
def team(plan):
    plan.paystack_amount_kes = plan.price_kes
    plan.save()
    return plan


CONSENT = "I agree to be charged KES 5,000 every month until I cancel."


def subscribe(user, team, fake, **kw):
    return subscriptions.start_subscription_checkout(user, team, CONSENT, provider=fake, **kw)


def pay_checkout(fake, user, company, **kw):
    """The customer pays on Paystack's page and comes back."""
    payment = Payment.objects.filter(company=company, kind="subscription").latest("created_at")
    fake.transactions[payment.reference] = transaction(payment.reference, **kw)
    fake.customer_subs = [paystack_sub()]
    fake.subs["SUB_1"] = paystack_sub()
    return subscriptions.card_return(user, payment.reference, provider=fake)


def webhook(client, event, data, secret=SECRET):
    body = json.dumps({"event": event, "data": data}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return client.post(reverse("billing:webhook", args=["paystack"]), body, content_type="application/json",
                       HTTP_X_PAYSTACK_SIGNATURE=sig)


@pytest.fixture
def active(subs_on, user, company, team, fake, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        subscribe(user, team, fake)
        pay_checkout(fake, user, company)
    mail.outbox.clear()
    return Subscription.objects.get(company=company, status=SS.ACTIVE)


# --- opening a checkout ---------------------------------------------------------------------------------------

def test_subscribing_opens_paystacks_checkout_with_the_plan(subs_on, user, company, team, fake, settings):
    settings.SITE_URL = "https://subra.co.ke"

    def rows_exist_first(body):
        assert Payment.objects.filter(reference=body["reference"], status="pending").exists()

    fake.on_init = rows_exist_first
    url = subscribe(user, team, fake)
    _, path, body = fake.calls[-1]
    assert path == "/transaction/initialize"
    assert body["plan"] == "PLN_team" and body["amount"] == 500000 and body["currency"] == "KES"
    assert body["channels"] == ["card"] and body["email"] == user.email
    assert body["callback_url"] == "https://subra.co.ke/billing/card/return/"
    assert body["reference"].startswith(f"subra-sub-{company.pk}-")
    assert url == f"https://checkout.paystack.com/{body['reference']}"
    sub = Subscription.objects.get()
    assert (sub.status, sub.plan, sub.consent_text) == (SS.INCOMPLETE, team, CONSENT) and sub.consented_at
    payment = Payment.objects.get()
    assert (payment.kind, payment.channel, payment.amount_kes, payment.subscription) == (
        "subscription", "card", 5000, sub)


def test_an_open_checkout_is_reused(subs_on, user, team, fake):
    first = subscribe(user, team, fake)
    assert subscribe(user, team, fake) == first
    assert fake.paths().count("/transaction/initialize") == 1
    assert Subscription.objects.count() == 1


def test_an_old_checkout_is_replaced(subs_on, user, team, fake):
    subscribe(user, team, fake)
    Payment.objects.update(created_at=timezone.now() - timedelta(hours=1))
    subscribe(user, team, fake)
    assert list(Subscription.objects.order_by("pk").values_list("status", flat=True)) == ["abandoned", "incomplete"]
    assert Payment.objects.filter(status="failed", result_code="abandoned").count() == 1


def test_a_paystack_outage_at_checkout(subs_on, user, team, fake):
    fake.down = True
    with pytest.raises(subscriptions.BillingError, match="couldn't reach Paystack"):
        subscribe(user, team, fake)
    assert Subscription.objects.get().status == SS.ABANDONED
    assert Payment.objects.get().status == "failed"


@pytest.mark.parametrize("problem, message", [
    ("flag", "aren't available"), ("member", "Only your company's owner"), ("consent", "Tick the box"),
    ("no_code", "isn't available"), ("out_of_sync", "being updated"), ("starter", "paused"),
])
def test_checkout_refusals(problem, message, settings, user, member, team, fake):
    settings.SUBSCRIPTIONS_ENABLED = problem != "flag"
    who, consent = (member if problem == "member" else user), ("" if problem == "consent" else CONSENT)
    if problem == "no_code":
        team.paystack_plan_code = ""
        team.save()
    if problem == "out_of_sync":
        team.price_kes = 5500
        team.save()
    if problem == "starter":
        settings.PAYSTACK_STARTER_LIMIT_KES = 5000
    with pytest.raises(subscriptions.BillingError, match=message):
        subscriptions.start_subscription_checkout(who, team, consent, provider=fake)
    assert not fake.paths()


def test_a_second_plan_is_refused(active, user, team, fake):
    with pytest.raises(subscriptions.BillingError, match="already has a plan"):
        subscribe(user, team, fake)


# --- the first payment ----------------------------------------------------------------------------------------

def test_paying_activates_grants_and_finds_the_paystack_subscription(subs_on, user, company, team, fake,
                                                                     django_capture_on_commit_callbacks):
    subscribe(user, team, fake)
    with django_capture_on_commit_callbacks(execute=True):
        payment = pay_checkout(fake, user, company)
    assert payment.status == "paid" and str(payment.fee_kes) == "145.00" and not payment.fee_estimated
    sub = Subscription.objects.get()
    assert sub.status == SS.ACTIVE and sub.periods_paid == 1
    assert (sub.paystack_customer_code, sub.card_last4, sub.card_exp) == ("CUS_1", "4081", "12/2030")
    assert (sub.paystack_subscription_code, sub.paystack_email_token) == ("SUB_1", "tok_1")
    assert sub.renews_at.isoformat().startswith("2026-11-27")
    assert credits.balance(company) == 115
    assert mail.outbox[0].subject == "Your SUBRA Team plan is active"
    assert "card ending 4081 will be charged KES 5,000 every month" in mail.outbox[0].body


def test_until_paid_the_checkout_stays_open(subs_on, user, company, team, fake):
    subscribe(user, team, fake)
    payment = Payment.objects.get()
    subscriptions.card_return(user, payment.reference, provider=fake)  # Paystack says "abandoned" until paid
    payment.refresh_from_db()
    assert payment.status == "pending" and Subscription.objects.get().status == SS.INCOMPLETE


def test_the_return_page_only_looks_in_your_own_company(subs_on, user, company, team, fake, client):
    from accounts.models import User

    subscribe(user, team, fake)
    stranger = User.objects.create_user("stranger@example.co.ke", "a-strong-password-1")
    assert subscriptions.card_return(stranger, Payment.objects.get().reference, provider=fake) is None


def test_the_return_view(active, client, user, company, team, fake):
    client.force_login(user)
    payment = Payment.objects.get(kind="subscription")
    resp = client.get(reverse("billing:card_return") + f"?reference={payment.reference}&trxref={payment.reference}")
    assert resp.status_code == 302 and resp["Location"] == reverse("accounts:plan")
    resp = client.get(reverse("billing:card_return") + "?reference=subra-sub-1-nothing", follow=True)
    assert "find that payment" in resp.content.decode()


def test_the_webhook_and_the_return_page_grant_once(active, client, company):
    payment = Payment.objects.get(kind="subscription")
    for _ in range(2):
        assert webhook(client, "charge.success", transaction(payment.reference)).status_code == 200
    assert credits.balance(company) == 115
    assert WebhookDelivery.objects.filter(event_type="charge.success").count() == 2


def test_a_late_payment_after_the_checkout_was_given_up_is_honoured(subs_on, user, company, team, fake, client):
    subscribe(user, team, fake)
    sub = Subscription.objects.get()
    subscriptions._abandon(sub, "test")
    payment = Payment.objects.get()
    fake.subs["SUB_1"] = paystack_sub()
    webhook(client, "charge.success", transaction(payment.reference))
    sub.refresh_from_db()
    assert sub.status == SS.ACTIVE and credits.balance(company) == 115


# --- renewals -----------------------------------------------------------------------------------------------

def test_a_renewal_charge_grants_the_month_under_the_cap(active, client, company, fund):
    fund(100)  # 215 now: the grant is clipped to 15
    resp = webhook(client, "charge.success", transaction("T_renewal_1"))
    assert resp.status_code == 200
    payment = Payment.objects.get(kind="renewal")
    assert (payment.subscription, payment.amount_kes, payment.status, payment.provider_ref) == (
        active, 5000, "paid", "T_renewal_1")
    assert credits.balance(company) == 230
    active.refresh_from_db()
    assert active.periods_paid == 2
    webhook(client, "charge.success", transaction("T_renewal_1"))
    assert credits.balance(company) == 230 and Payment.objects.filter(kind="renewal").count() == 1
    outcomes = list(WebhookDelivery.objects.filter(reference="T_renewal_1").order_by("pk").values_list("outcome", flat=True))
    assert outcomes == ["renewal recorded", "applied to payment"]


def test_a_charge_for_no_known_plan_alerts(active, client, settings):
    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    data = transaction("T_other")
    data["customer"]["customer_code"] = "CUS_unknown"
    webhook(client, "charge.success", data)
    assert not Payment.objects.filter(reference="T_other").exists()
    assert "no known plan" in mail.outbox[-1].subject


def test_a_failed_renewal_starts_seven_days_of_grace_and_credits_still_work(active, client, company, fake, user,
                                                                          order_factory, on_commit):
    fake.subs["SUB_1"] = paystack_sub(status="attention")
    webhook(client, "invoice.payment_failed", {"subscription": {"subscription_code": "SUB_1"}, "paid": False})
    active.refresh_from_db()
    assert active.status == SS.PAST_DUE
    assert timedelta(days=6, hours=23) < active.grace_ends_at - timezone.now() <= timedelta(days=7)
    assert mail.outbox[-1].subject == "We couldn't charge your card for SUBRA"
    assert "115 credits still work until" in mail.outbox[-1].body
    from accounts.models import company_for

    order = order_factory(status="ready", credit_cost=7, company=company_for(user))
    credits.pay_with_credits(order, user)
    assert credits.balance(company) == 108


def test_a_renewal_after_grace_starts_ends_it(active, client, fake):
    subscriptions.mark_past_due(active)
    fake.subs["SUB_1"] = paystack_sub(next_date="2026-12-27T09:00:00.000Z")
    webhook(client, "charge.success", transaction("T_renewal_2"))
    active.refresh_from_db()
    assert active.status == SS.ACTIVE and active.grace_ends_at is None and active.past_due_since is None


def test_a_renewal_for_an_ended_plan_alerts_and_grants_nothing(active, client, company, settings):
    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    Subscription.objects.filter(pk=active.pk).update(status=SS.LAPSED)
    webhook(client, "charge.success", transaction("T_late"))
    assert credits.balance(company) == 115
    assert "renewed an ended plan" in mail.outbox[-1].subject


# --- syncing from Paystack -----------------------------------------------------------------------------------

@pytest.mark.parametrize("paystack_status, expected", [
    ("active", SS.ACTIVE), ("attention", SS.PAST_DUE), ("non-renewing", SS.CANCELLING), ("cancelled", SS.CANCELLING),
])
def test_sync_maps_paystacks_status(active, fake, paystack_status, expected):
    fake.subs["SUB_1"] = paystack_sub(status=paystack_status)
    subscriptions.sync_subscription(active, provider=fake)
    active.refresh_from_db()
    assert active.status == expected


def test_sync_brings_a_past_due_plan_back_when_paystack_moved_the_date(active, fake):
    subscriptions.mark_past_due(active)
    fake.subs["SUB_1"] = paystack_sub(status="active", next_date="2026-12-27T09:00:00.000Z")
    subscriptions.sync_subscription(active, provider=fake)
    active.refresh_from_db()
    assert active.status == SS.ACTIVE and active.renews_at.isoformat().startswith("2026-12-27")


def test_sync_doesnt_clear_grace_without_a_new_payment_date(active, fake):
    subscriptions.mark_past_due(active)
    fake.subs["SUB_1"] = paystack_sub(status="active")  # same date as before: not paid yet
    subscriptions.sync_subscription(active, provider=fake)
    active.refresh_from_db()
    assert active.status == SS.PAST_DUE


def test_an_outage_during_a_webhook_never_changes_the_plan(active, client, fake):
    fake.down = True
    resp = webhook(client, "invoice.update", {"subscription": {"subscription_code": "SUB_1"}})
    assert resp.status_code == 200
    active.refresh_from_db()
    assert active.status == SS.ACTIVE
    assert "sync failed" in WebhookDelivery.objects.get(event_type="invoice.update").outcome


def test_subscription_create_attaches_the_code(subs_on, user, company, team, fake, client):
    subscribe(user, team, fake)
    payment = Payment.objects.get()
    fake.transactions[payment.reference] = transaction(payment.reference)
    fake.customer_subs = []  # Paystack hasn't listed it yet
    subscriptions.card_return(user, payment.reference, provider=fake)
    sub = Subscription.objects.get()
    assert sub.status == SS.ACTIVE and sub.paystack_subscription_code == ""
    fake.subs["SUB_9"] = paystack_sub(code="SUB_9")
    webhook(client, "subscription.create", {"subscription_code": "SUB_9", "plan": {"plan_code": "PLN_team"},
                                            "customer": {"id": 101, "customer_code": "CUS_1"}})
    sub.refresh_from_db()
    assert sub.paystack_subscription_code == "SUB_9" and sub.paystack_email_token == "tok_1"


def test_an_unmatched_paystack_subscription_alerts(subs_on, client, settings):
    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    webhook(client, "subscription.create", {"subscription_code": "SUB_x", "customer": {"customer_code": "CUS_x"}})
    assert "no SUBRA record" in mail.outbox[-1].subject


# --- other webhook events -----------------------------------------------------------------------------------

def test_a_refund_claws_back_the_credits(active, client, company):
    payment = Payment.objects.get(kind="subscription")
    webhook(client, "refund.processed", {"transaction_reference": payment.reference, "status": "processed"})
    payment.refresh_from_db()
    assert payment.status == "refunded" and credits.balance(company) == 0


def test_disputes_alert(subs_on, client, settings):
    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    webhook(client, "charge.dispute.create", {"transaction": {"reference": "T_1"}})
    assert "dispute" in mail.outbox[-1].subject


def test_every_delivery_is_stored_and_bad_signatures_are_not(subs_on, client):
    webhook(client, "customeridentification.success", {"customer_code": "CUS_1"})
    assert WebhookDelivery.objects.get().outcome == "ignored"
    assert webhook(client, "charge.success", {"reference": "x"}, secret="wrong").status_code == 403
    assert WebhookDelivery.objects.count() == 1


# --- top-ups ------------------------------------------------------------------------------------------------

def test_a_top_up_adds_credits_and_can_be_paid_with_mpesa(active, user, company, fake):
    pack = CreditPack.objects.get(credits=25)
    url = subscriptions.start_topup_checkout(user, pack, provider=fake)
    _, _, body = fake.calls[-1]
    assert body["channels"] == ["card", "mobile_money"] and body["amount"] == 115000 and "plan" not in body
    assert url.startswith("https://checkout.paystack.com/subra-top-")
    payment = Payment.objects.get(kind="topup")
    fake.transactions[payment.reference] = transaction(payment.reference, amount_kes=1150, plan_code="",
                                                       channel="mobile_money")
    subscriptions.card_return(user, payment.reference, provider=fake)
    payment.refresh_from_db()
    assert payment.status == "paid" and payment.channel == "mpesa"
    assert credits.balance(company) == 140


def test_top_ups_need_a_plan_and_the_owner(subs_on, user, member, company, fake):
    pack = CreditPack.objects.get(credits=10)
    with pytest.raises(subscriptions.BillingError, match="companies with a plan"):
        subscriptions.start_topup_checkout(user, pack, provider=fake)
    with pytest.raises(subscriptions.BillingError, match="owner"):
        subscriptions.start_topup_checkout(member, pack, provider=fake)


# --- changing plan, a new card, cancel and resume -----------------------------------------------------------

def test_changing_plan_starts_the_new_one_today_and_keeps_the_credits(active, user, company, fake,
                                                                      django_capture_on_commit_callbacks):
    business = Plan.objects.get(code="business")
    business.paystack_plan_code, business.paystack_amount_kes = "PLN_business", business.price_kes
    business.save()
    subscribe(user, business, fake, replace=True)
    new = Subscription.objects.get(status=SS.INCOMPLETE)
    assert new.replaces == active
    payment = Payment.objects.get(subscription=new)
    fake.transactions[payment.reference] = transaction(payment.reference, amount_kes=12000, plan_code="PLN_business")
    fake.customer_subs = [paystack_sub(code="SUB_2", plan_code="PLN_business"), paystack_sub()]
    fake.subs["SUB_2"] = paystack_sub(code="SUB_2", plan_code="PLN_business")
    with django_capture_on_commit_callbacks(execute=True):
        subscriptions.card_return(user, payment.reference, provider=fake)
    active.refresh_from_db()
    new.refresh_from_db()
    assert active.status == SS.CANCELLED and new.status == SS.ACTIVE
    assert new.paystack_subscription_code == "SUB_2"
    assert ("POST", "/subscription/disable", {"code": "SUB_1", "token": "tok_1"}) in fake.calls
    assert credits.balance(company) == 415  # 115 carried over + 300


def test_a_second_paid_plan_that_isnt_a_replacement_alerts(active, company, team, settings, client, fake):
    settings.ADMIN_ALERT_EMAIL = "ops@example.co.ke"
    stray = Subscription.objects.create(company=company, plan=team, email="x@example.co.ke")
    payment = Payment.objects.create(kind="subscription", channel="card", company=company, subscription=stray,
                                     provider_ref="subra-sub-x-1", reference="subra-sub-x-1", amount_kes=5000)
    webhook(client, "charge.success", transaction(payment.reference))
    assert credits.balance(company) == 115
    assert "Second subscription" in mail.outbox[-1].subject


def test_cancel_stops_charges_and_keeps_credits_until_period_end(active, user, fake, company, on_commit):
    sub = subscriptions.cancel(user, provider=fake)
    assert ("POST", "/subscription/disable", {"code": "SUB_1", "token": "tok_1"}) in fake.calls
    assert sub.status == SS.CANCELLING and sub.credits_usable_until == sub.renews_at
    assert credits.balance(company) == 115
    assert mail.outbox[-1].subject == "Your SUBRA plan is cancelled"
    sub = subscriptions.resume(user, provider=fake)
    assert sub.status == SS.ACTIVE and ("POST", "/subscription/enable", {"code": "SUB_1", "token": "tok_1"}) in fake.calls


def test_cancel_during_grace_ends_the_credits_when_grace_ends(active, user, fake):
    subscriptions.mark_past_due(active)
    sub = subscriptions.cancel(user, provider=fake)
    assert sub.status == SS.CANCELLING and sub.renews_at == sub.grace_ends_at
    with pytest.raises(subscriptions.BillingError, match="card payment failed"):
        subscriptions.resume(user, provider=fake)


def test_cancel_when_paystack_is_down_changes_nothing(active, user, fake):
    fake.down = True
    with pytest.raises(subscriptions.BillingError, match="couldn't reach Paystack"):
        subscriptions.cancel(user, provider=fake)
    active.refresh_from_db()
    assert active.status == SS.ACTIVE


def test_the_card_is_changed_on_paystacks_page(active, user, fake):
    assert subscriptions.card_update_link(user, provider=fake) == "https://paystack.com/manage/subscriptions/x"
    assert fake.calls[-1][1] == "/subscription/SUB_1/manage/link"


# --- plans at Paystack ----------------------------------------------------------------------------------------

def test_sync_plans_creates_and_updates(db, fake):
    results = dict((p.code, a) for p, a in subscriptions.sync_plans(provider=fake))
    assert results == {"solo": "created", "team": "created", "business": "created"}
    _, _, body = fake.calls[0]
    assert body == {"name": "SUBRA Solo", "amount": 200000, "interval": "monthly", "currency": "KES",
                    "send_invoices": True, "send_sms": False}
    assert Plan.objects.get(code="team").paystack_plan_code == "PLN_team"
    Plan.objects.filter(code="team").update(price_kes=5500)
    results = dict((p.code, a) for p, a in subscriptions.sync_plans(provider=fake))
    assert results["team"] == "updated" and results["solo"] == "unchanged"
    assert fake.calls[-1] == ("PUT", "/plan/PLN_team", {"name": "SUBRA Team", "amount": 550000, "currency": "KES",
                                                       "update_existing_subscriptions": True})
    assert Plan.objects.get(code="team").paystack_amount_kes == 5500
