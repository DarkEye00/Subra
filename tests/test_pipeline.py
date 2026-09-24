import copy

import pytest
from pptx import Presentation
from pptx.util import Inches

from deckbuilder import build_rich_deck
from polisher.ai import Usage
from polisher.heuristic import plan_deck
from polisher.pipeline import PlannerUnavailable, UnsupportedContentError, polish
from polisher.planner import HeuristicPlanner, PlanAttempt
from polisher.render.renderer import render
from polisher.validate import ValidationError


class ScriptedPlanner:
    """Returns a scripted sequence: "good" (heuristic plan), "bad" (no plan), or "outage"."""

    def __init__(self, script, model="claude-sonnet-5"):
        self.script = list(script)
        self.model = model
        self.feedback = []

    def plan(self, deck, feedback=None):
        self.feedback.append(feedback)
        step = self.script.pop(0)
        usage = Usage(100, 50)
        if step == "good":
            return PlanAttempt("claude", self.model, plan_deck(deck), usage, 0.01, 5)
        return PlanAttempt("claude", self.model, None, usage, 0.01, 5, error=f"{step} output",
                           retryable=(step == "outage"))


@pytest.fixture
def deck_path(tmp_path):
    return build_rich_deck(tmp_path / "in.pptx")


def test_heuristic_run_produces_a_verified_deck(tmp_path, deck_path):
    out = tmp_path / "out.pptx"
    result = polish(deck_path, out, theme="navy", planner=HeuristicPlanner())
    assert result.ok, result.failure
    assert out.exists()
    assert result.verification.passed
    assert len(result.attempts) == 1 and result.total_cost_usd == 0
    assert result.slide_count == 7


def test_bad_first_plan_is_retried_with_feedback(tmp_path, deck_path):
    planner = ScriptedPlanner(["bad", "good"])
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=planner)
    assert result.ok
    assert len(result.attempts) == 2
    assert planner.feedback[0] is None
    assert any("bad output" in f for f in planner.feedback[1])
    assert result.total_cost_usd == pytest.approx(0.02)


def test_escalates_to_the_stronger_model_after_two_failures(tmp_path, deck_path):
    planner = ScriptedPlanner(["bad", "bad"])
    escalation = ScriptedPlanner(["good"], model="claude-opus-5-5")
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=planner, escalation=escalation)
    assert result.ok
    assert [a.model for a in result.attempts] == ["claude-sonnet-5", "claude-sonnet-5", "claude-opus-5-5"]
    assert escalation.feedback[0]  # the escalation model is told what went wrong


def test_three_failures_fail_the_job_without_output(tmp_path, deck_path):
    out = tmp_path / "out.pptx"
    result = polish(deck_path, out, theme="navy", planner=ScriptedPlanner(["bad", "bad"]),
                    escalation=ScriptedPlanner(["bad"], model="claude-opus-5-5"))
    assert not result.ok
    assert len(result.attempts) == 3
    assert not out.exists()
    assert result.failure


def test_verification_failure_triggers_a_retry_with_the_problems(tmp_path, deck_path):
    calls = {"n": 0}

    def flaky_render(plan, deck, theme, output):
        calls["n"] += 1
        if calls["n"] == 1:  # simulate a renderer bug that loses an image
            deck = copy.deepcopy(deck)
            deck.slides[4].images[0].blob = build_png_variant()
        return render(plan, deck, theme, output)

    planner = ScriptedPlanner(["good", "good"])
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=planner, render_fn=flaky_render)
    assert result.ok
    assert result.attempts[0].verification_passed is False
    assert any("Image [s5-i1]" in f for f in planner.feedback[1])


def build_png_variant():
    from deckbuilder import png_bytes

    return png_bytes(400, 300, color=(0, 0, 255))


def test_unsupported_content_is_rejected_before_any_ai_call(tmp_path):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    movie = tmp_path / "clip.mp4"
    movie.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
    s.shapes.add_movie(str(movie), Inches(1), Inches(1), Inches(4), Inches(3), mime_type="video/mp4")
    prs.save(tmp_path / "v.pptx")
    planner = ScriptedPlanner([])
    with pytest.raises(UnsupportedContentError) as exc:
        polish(tmp_path / "v.pptx", tmp_path / "out.pptx", theme="navy", planner=planner)
    assert "video" in str(exc.value)
    assert planner.feedback == []


def test_invalid_file_is_rejected_before_any_ai_call(tmp_path):
    bad = tmp_path / "bad.pptx"
    bad.write_bytes(b"not a deck")
    planner = ScriptedPlanner([])
    with pytest.raises(ValidationError):
        polish(bad, tmp_path / "out.pptx", theme="navy", planner=planner)
    assert planner.feedback == []


def test_api_outage_defers_the_job_instead_of_failing_it(tmp_path, deck_path):
    with pytest.raises(PlannerUnavailable) as exc:
        polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=ScriptedPlanner(["outage"]))
    assert len(exc.value.attempts) == 1  # still logged, so its cost is counted


def test_before_attempt_hook_runs_before_each_ai_call(tmp_path, deck_path):
    seen = []
    polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=ScriptedPlanner(["bad", "good"]),
           before_attempt=lambda number, planner: seen.append(number))
    assert seen == [1, 2]
