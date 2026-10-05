import re
from datetime import timedelta

import pytest
from django.core.files.storage import default_storage
from django.urls import reverse
from django.utils import timezone
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from billing.models import Payment
from orders import processing, services, spend, tasks
from orders.models import AIJob, Order, Theme
from polisher.pipeline import RerenderResult
from polisher.pipeline import rerender as real_rerender

S = Order.Status
ERROR = "We couldn't switch to Clean Slate; your deck is unchanged."


@pytest.fixture(autouse=True)
def _previews(fake_previews):
    """Every test here fakes LibreOffice."""


@pytest.fixture
def capture(django_capture_on_commit_callbacks):
    return django_capture_on_commit_callbacks


@pytest.fixture
def logged_in(client, user):
    client.force_login(user)
    return client


def slate():
    return Theme.objects.get(slug="slate")


def switch(order, capture, slug="slate"):
    """Start a switch and run its task (Celery runs inline in tests)."""
    with capture(execute=True):
        services.start_theme_switch(order, Theme.objects.get(slug=slug))
    order.refresh_from_db()
    return order


def theme_name(order):
    with default_storage.open(order.result_file.name, "rb") as f:
        part = Presentation(f).slide_master.part.part_related_by(RT.THEME)
    return re.search(rb'<a:clrScheme name="([^"]*)"', part.blob).group(1).decode()


def failing_rerender(*args, **kwargs):
    return RerenderResult(ok=False, output_path=None, failure="Verification failed: Image [s5-i1] changed")


def test_switching_rerenders_the_saved_plan_without_ai(ready_order, capture):
    old_files = [ready_order.result_file.name, *ready_order.preview_images]
    jobs = AIJob.objects.count()
    order = switch(ready_order, capture)
    assert order.status == S.READY and order.theme.slug == "slate"
    assert order.switching_to is None and order.switch_error == ""
    assert theme_name(order) == "SUBRA Clean Slate"
    assert AIJob.objects.count() == jobs
    assert len(order.preview_images) == 2 and order.result_file.name not in old_files
    assert not any(default_storage.exists(n) for n in old_files)
    assert not order.is_paid


def test_switching_after_paying_keeps_the_deck_paid(paid_order, capture):
    order = switch(paid_order, capture)
    assert order.theme.slug == "slate" and order.is_paid and order.status == S.READY


def test_a_failed_switch_keeps_the_previous_deck(ready_order, capture, monkeypatch):
    monkeypatch.setattr(processing, "rerender", failing_rerender)
    result_file = ready_order.result_file.name
    order = switch(ready_order, capture)
    assert order.status == S.READY and order.theme.slug == "navy" and order.switching_to is None
    assert order.result_file.name == result_file and default_storage.exists(result_file)
    assert order.switch_error == ERROR


def test_a_crashed_switch_keeps_the_previous_deck(ready_order, capture, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("LibreOffice fell over")

    monkeypatch.setattr(processing, "rerender", crash)
    order = switch(ready_order, capture)
    assert order.status == S.READY and order.theme.slug == "navy" and order.switch_error == ERROR


def test_the_next_successful_switch_clears_the_error(ready_order, capture, monkeypatch):
    monkeypatch.setattr(processing, "rerender", failing_rerender)
    switch(ready_order, capture)
    monkeypatch.setattr(processing, "rerender", real_rerender)
    order = switch(ready_order, capture)
    assert order.theme.slug == "slate" and order.switch_error == ""


@pytest.mark.parametrize("why", ["same theme", "pending payment", "not generated", "no plan"])
def test_switches_that_dont_make_sense_are_refused(ready_order, order_factory, why):
    order, theme = ready_order, slate()
    if why == "same theme":
        theme = ready_order.theme
    elif why == "pending payment":
        Payment.objects.create(order=order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                               amount_kes=200)
    elif why == "not generated":
        order = order_factory("Quoted.pptx")
    elif why == "no plan":
        Order.objects.filter(pk=order.pk).update(plan=None)
    with pytest.raises(services.ThemeSwitchError):
        services.start_theme_switch(order, theme)


def test_a_stuck_switch_is_released_after_10_minutes(ready_order):
    Order.objects.filter(pk=ready_order.pk).update(status=S.RERENDERING, switching_to=slate(),
                                                   updated_at=timezone.now() - timedelta(minutes=11))
    tasks.release_queued_orders()
    ready_order.refresh_from_db()
    assert ready_order.status == S.READY and ready_order.switching_to is None
    assert ready_order.switch_error == ERROR


def test_stuck_switches_are_released_even_when_the_spend_cap_is_reached(ready_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    Order.objects.filter(pk=ready_order.pk).update(status=S.RERENDERING, switching_to=slate(),
                                                   updated_at=timezone.now() - timedelta(minutes=11))
    tasks.release_queued_orders()
    ready_order.refresh_from_db()
    assert ready_order.status == S.READY


def test_a_recent_switch_is_left_alone(ready_order):
    Order.objects.filter(pk=ready_order.pk).update(status=S.RERENDERING, switching_to=slate())
    tasks.release_queued_orders()
    ready_order.refresh_from_db()
    assert ready_order.status == S.RERENDERING


def test_a_switch_that_finishes_after_being_released_is_discarded(ready_order):
    services.start_theme_switch(ready_order, slate())  # on_commit doesn't fire here, so the task hasn't run
    started = Order.objects.select_related("switching_to").get(pk=ready_order.pk)
    processing.finish_switch_failed(started, started.switching_to, "Timed out")  # what the release does
    result_file = Order.objects.get(pk=ready_order.pk).result_file.name
    assert processing.run_rerender(started) is False
    order = Order.objects.get(pk=ready_order.pk)
    assert order.status == S.READY and order.theme.slug == "navy" and order.result_file.name == result_file


def test_a_payment_that_arrives_during_a_switch_is_accepted(ready_order, confirm_payment):
    services.start_theme_switch(ready_order, slate())
    order = confirm_payment(Order.objects.get(pk=ready_order.pk))
    assert order.status == S.RERENDERING and order.is_paid


# --- the page ---------------------------------------------------------------

def test_theme_buttons_start_a_switch(logged_in, ready_order):
    html = logged_in.get(reverse("orders:detail", args=[ready_order.public_id])).content.decode()
    assert "Switch to Clean Slate" in html and "Switch to Navy Executive" not in html
    resp = logged_in.post(reverse("orders:theme", args=[ready_order.public_id]), {"theme": "slate"}, follow=True)
    assert "Switching to Clean Slate" in resp.content.decode()
    ready_order.refresh_from_db()
    assert ready_order.status == S.RERENDERING


def test_theme_switches_are_rate_limited(logged_in, ready_order, settings):
    settings.THEME_SWITCH_RATE_LIMIT = (1, 3600)
    logged_in.post(reverse("orders:theme", args=[ready_order.public_id]), {"theme": "slate"})
    Order.objects.filter(pk=ready_order.pk).update(status=S.READY, switching_to=None)
    resp = logged_in.post(reverse("orders:theme", args=[ready_order.public_id]), {"theme": "savanna"}, follow=True)
    assert "switched themes a lot in the last hour" in resp.content.decode()


def test_theme_buttons_wait_for_a_pending_payment(logged_in, ready_order):
    Payment.objects.create(order=ready_order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=200)
    html = logged_in.get(reverse("orders:detail", args=[ready_order.public_id])).content.decode()
    assert "You can switch themes once the payment has gone through." in html


def test_a_failed_switch_is_explained_on_the_page(logged_in, ready_order):
    Order.objects.filter(pk=ready_order.pk).update(switch_error=ERROR)
    html = logged_in.get(reverse("orders:detail", args=[ready_order.public_id])).content.decode()
    assert "switch to Clean Slate; your deck is unchanged." in html
