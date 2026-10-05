import copy
import re

import pytest
from pptx import Presentation
from pptx.util import Inches

from deckbuilder import build_rich_deck
from polisher.ai import Usage
from polisher.heuristic import plan_deck
from polisher.pipeline import PlannerUnavailable, polish, rerender
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

    def flaky_render(plan, deck, theme, output, source):
        report = render(plan, deck, theme, output, source)
        calls["n"] += 1
        if calls["n"] == 1:  # simulate a renderer bug that swaps an image
            from pptx import Presentation

            prs = Presentation(output)
            pic = next(sh for slide in prs.slides for sh in slide.shapes if sh.shape_type == 13)
            pic.image  # noqa: B018 (make sure it's a picture)
            rid = pic._element.blipFill.blip.rEmbed
            pic.part.related_part(rid)._blob = build_png_variant()
            prs.save(output)
        return report

    planner = ScriptedPlanner(["good", "good"])
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=planner, render_fn=flaky_render)
    assert result.ok
    assert result.attempts[0].verification_passed is False
    assert any("Image [s5-i1]" in f for f in planner.feedback[1])


def build_png_variant():
    from deckbuilder import png_bytes

    return png_bytes(400, 300, color=(0, 0, 255))


def test_video_decks_are_polished_with_the_video_kept(tmp_path):
    from lockedbuilder import build_locked_deck

    src = build_locked_deck(tmp_path / "locked.pptx")
    result = polish(src, tmp_path / "out.pptx", theme="navy", planner=HeuristicPlanner())
    assert result.ok, result.attempts[-1].problems
    assert result.verification.counts["locked"] == 9


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


def test_on_attempt_reports_each_attempt_as_it_finishes(tmp_path, deck_path):
    seen = []
    polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=ScriptedPlanner(["bad", "good"]),
           on_attempt=lambda record: seen.append((record.number, record.error, record.verification_passed)))
    assert seen == [(1, "bad output", None), (2, None, True)]


def test_on_attempt_sees_the_attempt_before_an_outage_is_raised(tmp_path, deck_path):
    seen = []
    with pytest.raises(PlannerUnavailable):
        polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=ScriptedPlanner(["outage"]),
               on_attempt=seen.append)
    assert len(seen) == 1


def _theme_name(path):
    """The colour scheme name apply_theme writes, e.g. "SUBRA Clean Slate"."""
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    part = Presentation(path).slide_master.part.part_related_by(RT.THEME)
    return re.search(rb'<a:clrScheme name="([^"]*)"', part.blob).group(1).decode()


def test_a_successful_run_exposes_its_verified_plan(tmp_path, deck_path):
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=HeuristicPlanner())
    assert result.plan is not None and result.plan == result.attempts[-1].plan
    assert len(result.plan["slides"]) == 7


def test_a_failed_run_has_no_plan(tmp_path, deck_path):
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=ScriptedPlanner(["bad", "bad"]))
    assert not result.ok and result.plan is None


def test_rerender_applies_another_theme_to_a_saved_plan(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())
    out = tmp_path / "slate.pptx"
    result = rerender(deck_path, out, "slate", first.plan)
    assert result.ok, result.failure
    assert result.verification.passed
    assert _theme_name(out) == "SUBRA Clean Slate"
    assert _theme_name(tmp_path / "navy.pptx") == "SUBRA Navy Executive"


def test_rerender_refuses_a_plan_that_no_longer_fits(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())
    broken = copy.deepcopy(first.plan)
    broken["slides"] = broken["slides"][:1]
    out = tmp_path / "out.pptx"
    result = rerender(deck_path, out, "slate", broken)
    assert not result.ok and "s2-t1" in result.failure
    assert not out.exists()


def test_rerender_refuses_an_unreadable_plan(tmp_path, deck_path):
    result = rerender(deck_path, tmp_path / "out.pptx", "slate", {"pages": []})
    assert not result.ok and "unreadable" in result.failure


def test_rerender_reports_a_verification_failure(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())

    def lossy_render(plan, deck, theme, output, source):
        report = render(plan, deck, theme, output, source)
        prs = Presentation(output)
        pic = next(sh for slide in prs.slides for sh in slide.shapes if sh.shape_type == 13)
        pic.part.related_part(pic._element.blipFill.blip.rEmbed)._blob = build_png_variant()
        prs.save(output)
        return report

    out = tmp_path / "out.pptx"
    result = rerender(deck_path, out, "slate", first.plan, render_fn=lossy_render)
    assert not result.ok and "Image [s5-i1]" in result.failure
    assert not out.exists()


def test_rerender_survives_a_renderer_crash(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())

    def crash(*args):
        raise RuntimeError("boom")

    out = tmp_path / "out.pptx"
    result = rerender(deck_path, out, "slate", first.plan, render_fn=crash)
    assert not result.ok and result.failure == "Render failed: RuntimeError: boom"
    assert not out.exists()
