import os
from datetime import timedelta
from pathlib import Path

import pytest
from django.core import mail
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from pptx import Presentation

from billing.models import Payment
from deckbuilder import build_rich_deck, build_simple_deck
from orders import processing, services, spend, tasks
from orders.models import AIJob, DailySpend, Order
from polisher.ai import Usage
from polisher.heuristic import plan_deck
from polisher.planner import PlanAttempt

S = Order.Status


@pytest.fixture(autouse=True)
def _previews(fake_previews):
    """Every test here fakes LibreOffice."""


def upload(path, name="Q3 review.pptx"):
    return SimpleUploadedFile(name, Path(path).read_bytes(),
                              content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@pytest.fixture
def queued_order(user, tmp_path):
    return services.start_generation(services.create_order(user, upload(build_rich_deck(tmp_path / "rich.pptx"))))


class CostlyPlanner:
    """Heuristic plans, but each attempt 'costs' like a Claude call."""

    def __init__(self, outcomes, model="claude-sonnet-5", cost=0.05):
        self.outcomes, self.model, self.cost = list(outcomes), model, cost

    def plan(self, deck, feedback=None):
        good = self.outcomes.pop(0) == "good"
        return PlanAttempt("claude", self.model, plan_deck(deck) if good else None, Usage(5000, 2000),
                           self.cost, 10, error=None if good else "unusable plan")


def use_planners(monkeypatch, planner, escalation=None):
    monkeypatch.setattr(processing, "build_planners", lambda: (planner, escalation))


# --- upload and quote -------------------------------------------------------

def test_upload_creates_a_quoted_order(user, tmp_path):
    order = services.create_order(user, upload(build_rich_deck(tmp_path / "d.pptx")))
    assert order.status == S.QUOTED
    assert (order.slide_count, order.tier.name, order.price_kes) == (7, "Small", 200)
    assert order.original_name == "Q3 review.pptx"
    assert default_storage.exists(order.original_file.name)
    assert order.theme.slug == "navy"
    assert order.events.get().note == "7 slides, Small tier, KES 200"


def test_upload_rejects_non_pptx(user, tmp_path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"%PDF-1.4")
    with pytest.raises(services.UploadError) as exc:
        services.create_order(user, upload(bad, "x.pdf"))
    assert ".pptx" in exc.value.message
    assert not Order.objects.exists()


def test_upload_rejects_decks_over_the_slide_limit(user, tmp_path, settings):
    settings.MAX_SLIDES = 5
    with pytest.raises(services.UploadError) as exc:
        services.create_order(user, upload(build_simple_deck(tmp_path / "d.pptx", n_slides=6)))
    assert "6 slides" in exc.value.message


def test_deck_with_locked_objects_is_quoted_with_a_summary(user, tmp_path):
    from lockedbuilder import build_locked_deck

    order = services.create_order(user, upload(build_locked_deck(tmp_path / "locked.pptx")))
    assert order.status == S.QUOTED
    summary = order.locked_summary
    assert summary["slides"] == 9 and summary["linked"] == 1 and summary["counts"]["video"] == 1
    assert order.locked_description.startswith("9 slides contain ")
    assert "embedded objects" in order.locked_description and "equations" in order.locked_description


def test_media_decks_get_the_larger_upload_limit(user, tmp_path, settings):
    from lockedbuilder import build_locked_deck

    settings.MAX_UPLOAD_BYTES, settings.MAX_UPLOAD_BYTES_MEDIA = 1000, 10_000_000
    order = services.create_order(user, upload(build_locked_deck(tmp_path / "locked.pptx")))
    assert order.status == S.QUOTED
    with pytest.raises(services.UploadError):
        services.create_order(user, upload(build_rich_deck(tmp_path / "rich.pptx")))


# --- generation -------------------------------------------------------------

def test_generation_produces_a_ready_deck_that_isnt_paid_for(queued_order):
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.READY, order.failure_reason
    assert not order.is_paid
    with default_storage.open(order.result_file.name, "rb") as f:
        assert len(Presentation(f).slides) == 7
    assert len(order.preview_images) == 2 and all(default_storage.exists(n) for n in order.preview_images)
    assert order.plan and len(order.plan["slides"]) == 7
    assert timedelta(hours=47) < order.expires_at - timezone.now() <= timedelta(hours=48)
    assert [e.to_status for e in order.events.all()][-2:] == [S.PROCESSING, S.READY]


def test_ai_cost_is_logged_per_attempt_with_fx(queued_order, monkeypatch, settings):
    settings.USD_KES_RATE, settings.CARD_FX_MARKUP = 100.0, 0.03
    use_planners(monkeypatch, CostlyPlanner(["bad", "good"]))
    tasks.process_order(queued_order.pk)
    jobs = list(AIJob.objects.filter(order=queued_order))
    assert [(j.attempt_number, j.model, j.input_tokens) for j in jobs] == [(1, "claude-sonnet-5", 5000),
                                                                          (2, "claude-sonnet-5", 5000)]
    assert float(jobs[0].cost_kes) == pytest.approx(0.05 * 100 * 1.03)
    assert jobs[0].verification_passed is None and jobs[1].verification_passed is True
    assert float(DailySpend.objects.get().total_cost_usd) == pytest.approx(0.10)
    assert float(Order.objects.get(pk=queued_order.pk).ai_cost_kes) == pytest.approx(0.10 * 103, abs=0.01)


def test_failed_generation_costs_the_customer_nothing_and_alerts_admins(queued_order, monkeypatch, settings):
    settings.USD_KES_RATE, settings.CARD_FX_MARKUP = 100.0, 0.03
    use_planners(monkeypatch, CostlyPlanner(["bad", "bad"]), CostlyPlanner(["bad"], model="claude-opus-5-5"))
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.FAILED and order.failure_reason
    assert not Payment.objects.exists()
    assert timedelta(hours=47) < order.expires_at - timezone.now() <= timedelta(hours=48)
    alert = next(m for m in mail.outbox if "failed to generate" in m.subject)
    assert "wanjiku@example.co.ke" in alert.body and "3 attempt(s)" in alert.body
    assert "AI cost: KES 15.45" in alert.body and "no refund" in alert.body


def test_spend_cap_keeps_orders_queued(queued_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    tasks.process_order(queued_order.pk)
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.QUEUED and order.waiting_for_capacity
    assert len([m for m in mail.outbox if "cap" in m.subject.lower()]) == 1  # alert once a day


def test_spend_cap_hit_mid_job_requeues(queued_order, monkeypatch, settings):
    settings.AI_DAILY_CAP_USD = 0.06
    use_planners(monkeypatch, CostlyPlanner(["bad", "good"], cost=0.07))
    tasks.process_order(queued_order.pk)
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.QUEUED and order.waiting_for_capacity
    assert AIJob.objects.filter(order=queued_order).count() == 1


def test_queued_orders_are_released_when_capacity_returns(queued_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    tasks.process_order(queued_order.pk)
    settings.AI_DAILY_CAP_USD = 5.0
    tasks.release_queued_orders()
    assert Order.objects.get(pk=queued_order.pk).status == S.READY


# --- file retention ---------------------------------------------------------

def test_expired_decks_are_really_deleted(ready_order):
    paths = [default_storage.path(n) for n in
             [ready_order.original_file.name, ready_order.result_file.name, *ready_order.preview_images]]
    assert all(os.path.exists(p) for p in paths)
    Order.objects.filter(pk=ready_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert not any(os.path.exists(p) for p in paths)
    assert ready_order.status == S.EXPIRED and ready_order.files_deleted_at
    assert ready_order.preview_images == [] and not ready_order.result_file and not ready_order.original_file


@pytest.mark.parametrize("status", [S.QUEUED, S.PROCESSING, S.RERENDERING])
def test_decks_being_worked_on_keep_their_files(ready_order, status):
    Order.objects.filter(pk=ready_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1), status=status)
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert default_storage.exists(ready_order.original_file.name) and not ready_order.files_deleted_at


def test_a_pending_payment_holds_off_deletion(ready_order):
    Payment.objects.create(order=ready_order, provider="intasend", provider_ref="INV-1", phone="254712345678",
                           amount_kes=200)
    Order.objects.filter(pk=ready_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert ready_order.status == S.READY and not ready_order.files_deleted_at


def test_unexpired_decks_keep_their_files(ready_order):
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert not ready_order.files_deleted_at


@pytest.mark.parametrize("change", ["paid", "switching"])
def test_the_sweep_rechecks_each_deck_after_locking_it(ready_order, monkeypatch, change):
    """The sweep reads its list once; a deck paid for, or switching theme, since then must be left alone."""
    from orders.models import Theme

    Order.objects.filter(pk=ready_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    read_list = tasks._expired_ids

    def list_then_change():
        ids = read_list()
        if change == "paid":
            Order.objects.filter(pk=ready_order.pk).update(paid_at=timezone.now(),
                                                           expires_at=timezone.now() + timedelta(days=7))
        else:
            Order.objects.filter(pk=ready_order.pk).update(status=S.RERENDERING,
                                                           switching_to=Theme.objects.get(slug="slate"))
        return ids

    monkeypatch.setattr(tasks, "_expired_ids", list_then_change)
    tasks.delete_expired_files()
    ready_order.refresh_from_db()
    assert not ready_order.files_deleted_at and default_storage.exists(ready_order.result_file.name)


# --- a worker that dies mid-job ---------------------------------------------

def stall(order, minutes):
    Order.objects.filter(pk=order.pk).update(status=S.PROCESSING, updated_at=timezone.now() - timedelta(minutes=minutes))


def test_a_generation_stalled_by_a_dead_worker_is_restarted(queued_order):
    stall(queued_order, 31)
    tasks.release_queued_orders()
    order = Order.objects.get(pk=queued_order.pk)
    assert order.status == S.READY  # requeued, and Celery runs inline in tests
    assert order.events.filter(note=tasks.RESTARTED).count() == 1


def test_a_generation_that_stalls_twice_fails_and_alerts(queued_order):
    from orders.models import OrderEvent

    OrderEvent.objects.create(order=queued_order, from_status=S.PROCESSING, to_status=S.QUEUED, note=tasks.RESTARTED)
    stall(queued_order, 31)
    tasks.release_queued_orders()
    assert Order.objects.get(pk=queued_order.pk).status == S.FAILED
    assert any("failed to generate" in m.subject for m in mail.outbox)


def test_a_generation_still_logging_attempts_is_left_alone(queued_order):
    stall(queued_order, 31)
    AIJob.objects.create(order=queued_order, attempt_number=1)  # an attempt finished just now
    tasks.release_queued_orders()
    assert Order.objects.get(pk=queued_order.pk).status == S.PROCESSING


def test_a_recent_generation_is_left_alone(queued_order):
    stall(queued_order, 5)
    tasks.release_queued_orders()
    assert Order.objects.get(pk=queued_order.pk).status == S.PROCESSING
