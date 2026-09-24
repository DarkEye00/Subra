import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from django.core import mail
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from pptx import Presentation
from pptx.util import Inches

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tests"))
from deckbuilder import build_rich_deck, build_simple_deck  # noqa: E402

from orders import processing, services, spend, tasks  # noqa: E402
from orders.models import AIJob, DailySpend, Order, Theme  # noqa: E402
from polisher.ai import Usage  # noqa: E402
from polisher.heuristic import plan_deck  # noqa: E402
from polisher.planner import PlanAttempt  # noqa: E402

S = Order.Status


def upload(path, name="Q3 review.pptx"):
    return SimpleUploadedFile(name, Path(path).read_bytes(),
                              content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@pytest.fixture(autouse=True)
def fast_previews(monkeypatch, tmp_path):
    """LibreOffice is slow; fake two thumbnails unless a test asks for the real thing."""
    def fake(pptx, out_dir, dpi=60):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i in (1, 2):
            p = out_dir / f"slide-{i}.png"
            p.write_bytes(b"\x89PNG fake")
            paths.append(p)
        return paths
    monkeypatch.setattr(processing, "render_previews", fake)


@pytest.fixture
def paid_order(user, tmp_path):
    order = services.create_order(user, upload(build_rich_deck(tmp_path / "rich.pptx")))
    order.transition(S.PAYMENT_PENDING)
    order.transition(S.PAID)
    return order


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


def test_upload_rejects_unsupported_content_before_payment(user, tmp_path):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    movie = tmp_path / "clip.mp4"
    movie.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    s.shapes.add_movie(str(movie), Inches(1), Inches(1), Inches(4), Inches(3), mime_type="video/mp4")
    prs.save(tmp_path / "v.pptx")
    with pytest.raises(services.UploadError) as exc:
        services.create_order(user, upload(tmp_path / "v.pptx"))
    assert "video" in exc.value.message


# --- processing -------------------------------------------------------------

def test_paid_order_is_polished(paid_order):
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    assert paid_order.status == S.DONE, paid_order.failure_reason
    with default_storage.open(paid_order.result_file.name, "rb") as f:
        assert len(Presentation(f).slides) == 7
    assert len(paid_order.preview_images) == 2
    assert all(default_storage.exists(n) for n in paid_order.preview_images)
    assert paid_order.expires_at > timezone.now() + timedelta(days=6)
    assert [e.to_status for e in paid_order.events.all()][-2:] == [S.PROCESSING, S.DONE]


def test_ai_cost_is_logged_per_attempt_with_fx(paid_order, monkeypatch, settings):
    settings.USD_KES_RATE, settings.CARD_FX_MARKUP = 100.0, 0.03
    use_planners(monkeypatch, CostlyPlanner(["bad", "good"]))
    tasks.process_order(paid_order.pk)
    jobs = list(AIJob.objects.filter(order=paid_order))
    assert [(j.attempt_number, j.model, j.input_tokens) for j in jobs] == [(1, "claude-sonnet-5", 5000), (2, "claude-sonnet-5", 5000)]
    assert float(jobs[0].cost_kes) == pytest.approx(0.05 * 100 * 1.03)
    assert jobs[0].verification_passed is None and jobs[1].verification_passed is True
    assert float(DailySpend.objects.get().total_cost_usd) == pytest.approx(0.10)
    paid_order.refresh_from_db()
    assert float(paid_order.ai_cost_kes) == pytest.approx(0.10 * 103, abs=0.01)


def test_failed_processing_goes_to_refund_pending(paid_order, monkeypatch):
    use_planners(monkeypatch, CostlyPlanner(["bad", "bad"]), CostlyPlanner(["bad"], model="claude-opus-5-5"))
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    assert paid_order.status == S.REFUND_PENDING
    assert paid_order.failure_reason
    assert AIJob.objects.filter(order=paid_order).count() == 3
    assert any("refund" in m.subject.lower() for m in mail.outbox)


def test_spend_cap_queues_paid_orders(paid_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    tasks.process_order(paid_order.pk)
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    assert paid_order.status == S.PAID and paid_order.waiting_for_capacity
    assert len([m for m in mail.outbox if "cap" in m.subject.lower()]) == 1  # alert once a day


def test_spend_cap_hit_mid_job_requeues(paid_order, monkeypatch, settings):
    settings.AI_DAILY_CAP_USD = 0.06
    use_planners(monkeypatch, CostlyPlanner(["bad", "good"], cost=0.07))
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    assert paid_order.status == S.PAID and paid_order.waiting_for_capacity
    assert AIJob.objects.filter(order=paid_order).count() == 1


def test_queued_orders_are_released_when_capacity_returns(paid_order, settings):
    settings.AI_DAILY_CAP_USD = 1.0
    spend.record_spend(1.5)
    tasks.process_order(paid_order.pk)
    settings.AI_DAILY_CAP_USD = 5.0
    tasks.release_queued_orders()
    paid_order.refresh_from_db()
    assert paid_order.status == S.DONE


# --- free re-run ------------------------------------------------------------

def test_free_rerun_with_another_theme(paid_order, django_capture_on_commit_callbacks):
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    first_result = paid_order.result_file.name
    with django_capture_on_commit_callbacks(execute=True):
        services.start_rerun(paid_order, Theme.objects.get(slug="slate"))
    paid_order.refresh_from_db()
    assert paid_order.status == S.DONE and paid_order.theme.slug == "slate"
    assert paid_order.rerun_used and not paid_order.can_rerun
    assert paid_order.result_file.name != first_result
    assert not default_storage.exists(first_result)


def test_failed_rerun_keeps_the_first_result(paid_order, monkeypatch, django_capture_on_commit_callbacks):
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    first_result = paid_order.result_file.name
    use_planners(monkeypatch, CostlyPlanner(["bad", "bad"]), CostlyPlanner(["bad"]))
    with django_capture_on_commit_callbacks(execute=True):
        services.start_rerun(paid_order, Theme.objects.get(slug="slate"))
    paid_order.refresh_from_db()
    assert paid_order.status == S.DONE
    assert paid_order.result_file.name == first_result
    assert paid_order.can_rerun  # they can try again


def test_rerun_only_once(paid_order):
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    services.start_rerun(paid_order, Theme.objects.get(slug="slate"))
    with pytest.raises(services.RerunError):
        services.start_rerun(paid_order, Theme.objects.get(slug="savanna"))


# --- file retention ---------------------------------------------------------

def test_expired_files_are_really_deleted(paid_order):
    tasks.process_order(paid_order.pk)
    paid_order.refresh_from_db()
    paths = [default_storage.path(n) for n in
             [paid_order.original_file.name, paid_order.result_file.name, *paid_order.preview_images]]
    assert all(os.path.exists(p) for p in paths)
    Order.objects.filter(pk=paid_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    tasks.delete_expired_files()
    paid_order.refresh_from_db()
    assert not any(os.path.exists(p) for p in paths)
    assert paid_order.status == S.EXPIRED and paid_order.files_deleted_at
    assert paid_order.preview_images == [] and not paid_order.result_file and not paid_order.original_file


def test_unexpired_and_in_flight_orders_keep_their_files(paid_order, user, tmp_path):
    Order.objects.filter(pk=paid_order.pk).update(expires_at=timezone.now() - timedelta(minutes=1),
                                                  status=S.PROCESSING)
    fresh = services.create_order(user, upload(build_rich_deck(tmp_path / "f.pptx")))
    tasks.delete_expired_files()
    for o in (paid_order, fresh):
        o.refresh_from_db()
        assert default_storage.exists(o.original_file.name)
