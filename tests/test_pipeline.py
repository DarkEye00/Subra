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

    def plan(self, deck, feedback=None, labels=None):
        self.feedback.append(feedback)
        self.labels = getattr(self, "labels", []) + [labels]
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


# --- the repair loop (17 Oct build plan, stages 6 and 7) ------------------

def _long_deck(tmp_path, paragraphs=20, picture=False):
    """A cover and one slide with far more text than fits on it (optionally a picture slide after)."""
    import io

    from deckbuilder import png_bytes

    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Cover"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Everything"
    tf = s.placeholders[1].text_frame
    tf.text = "Point 0: " + "detail " * 30
    for i in range(1, paragraphs):
        tf.add_paragraph().text = f"Point {i}: " + "detail " * 30
    if picture:
        p = prs.slides.add_slide(prs.slide_layouts[6])
        p.shapes.add_picture(io.BytesIO(png_bytes()), Inches(1), Inches(1), Inches(4))
    prs.save(tmp_path / "long.pptx")
    return tmp_path / "long.pptx"


class CrammingPlanner:
    """Plans every slide as one part (the long slide overflows); repair() behaves as scripted."""

    name, model = "claude", "claude-sonnet-5"

    def __init__(self, repair="fallback"):
        self.repair_mode = repair
        self.repairs = []

    def plan(self, deck, feedback=None, labels=None):
        from polisher.plan import Plan, PlannedSlide, _plannable

        items = _plannable(deck)
        slides = []
        for s in deck.slides:
            own = [items[i] for i in s.order if i in items]
            titles = [t.id for t in own if t.kind == "text" and t.role == "title"]
            visuals = [v.id for v in own if v.kind != "text"]
            body = [t.id for t in own if t.kind == "text" and t.id not in titles]
            if s.number == 1:
                slides.append(PlannedSlide(1, "title", {"title": titles, "subtitle": body}))
            elif visuals:
                slides.append(PlannedSlide(s.number, "image_text", {"visuals": visuals}))
            else:
                slides.append(PlannedSlide(s.number, "bullets", {"title": titles, "body": body}))
        return PlanAttempt("claude", self.model, Plan(slides), Usage(100, 50), 0.01, 5)

    def repair(self, deck, plan, faults, labels=None, attempt=1):
        from polisher.plan import Plan

        self.repairs.append(faults)
        if self.repair_mode == "fallback":
            return HeuristicPlanner().repair(deck, plan, faults, attempt=attempt)
        if self.repair_mode == "same":
            same = [ps for ps in plan.slides if ps.source_slide in faults]
            return PlanAttempt("claude", self.model, Plan(same), Usage(100, 50), 0.01, 5)
        return PlanAttempt("claude", self.model, None, Usage(100, 50), 0.01, 5, error="refusal")


def test_overflowing_slide_is_repaired_by_the_planner(tmp_path):
    planner = CrammingPlanner()
    out = tmp_path / "out.pptx"
    result = polish(_long_deck(tmp_path), out, theme="navy", planner=planner)
    assert result.ok and result.repair_passes == 1
    assert [a.stage for a in result.attempts] == ["plan", "repair"]
    assert planner.repairs[0] and 2 in planner.repairs[0]
    report = result.qa_report
    assert report["repaired_slides"] and report["flagged_slides"] == [] and report["fallback_slides"] == []
    assert len(result.plan["slides"]) > 2
    assert result.total_cost_usd == pytest.approx(0.01)  # the heuristic repair is free


def test_two_failed_repairs_fall_back_and_flag_the_slide(tmp_path):
    planner = CrammingPlanner(repair="same")
    result = polish(_long_deck(tmp_path), tmp_path / "out.pptx", theme="navy", planner=planner)
    assert result.ok and result.repair_passes == 2 and len(planner.repairs) == 2
    report = result.qa_report
    assert report["fallback_slides"] and report["repaired_slides"] == []
    assert report["flagged_slides"] == []  # the plainest layout split the text until it fit
    assert [a.stage for a in result.attempts] == ["plan", "repair", "repair"]


def test_heuristic_planner_repairs_by_splitting(tmp_path):
    # one 4,000-character paragraph can't be split, so the slide stays flagged, but the run never fails
    src = _long_deck(tmp_path, paragraphs=1)
    prs = Presentation(src)
    prs.slides[1].placeholders[1].text_frame.paragraphs[0].runs[0].text = "detail " * 570
    prs.save(src)
    result = polish(src, tmp_path / "out.pptx", theme="navy", planner=HeuristicPlanner())
    assert result.ok and result.verification.passed
    assert result.qa_report["flagged_slides"] == [2]
    assert result.qa_report["slides"][1]["faults"][0]["kind"] == "overflow"
    assert result.overflow_slides == [2]


def test_repair_that_breaks_verification_is_discarded(tmp_path):
    calls = {"n": 0}

    def flaky_render(plan, deck, theme, output, source):
        report = render(plan, deck, theme, output, source)
        calls["n"] += 1
        if calls["n"] == 2:  # the repair pass loses a picture
            prs = Presentation(output)
            pic = next(sh for slide in prs.slides for sh in slide.shapes if sh.shape_type == 13)
            pic.part.related_part(pic._element.blipFill.blip.rEmbed)._blob = build_png_variant()
            prs.save(output)
        return report

    out = tmp_path / "out.pptx"
    src = _long_deck(tmp_path, picture=True)
    result = polish(src, out, theme="navy", planner=CrammingPlanner(), render_fn=flaky_render)
    assert result.ok and result.repair_passes == 1
    repair = result.attempts[1]
    assert repair.stage == "repair" and repair.verification_passed is False and repair.plan is None
    from polisher.extract import extract_deck
    from polisher.verify import verify

    assert verify(extract_deck(src), extract_deck(out)).passed
    assert result.qa_report["fallback_slides"]  # the deterministic fallback still ran afterwards


def test_qa_report_shape_and_timings(tmp_path, deck_path):
    result = polish(deck_path, tmp_path / "out.pptx", theme="navy", planner=HeuristicPlanner())
    report = result.qa_report
    assert report["retention"]["passed"] is True and report["retention"]["texts"] == 13
    assert len(report["slides"]) == 7 and report["slides"][0]["layout"] == "title"
    assert set(report["timings_ms"]) >= {"extract", "plan", "qa", "total"} and report["vision_qa"] is False
    assert set(report["cost_usd"]) == {"classify", "plan", "repair", "vision", "total"}
    assert report["repair_passes"] == 0 and report["labels"] is None


def test_before_attempt_runs_for_repairs_too(tmp_path):
    seen = []
    polish(_long_deck(tmp_path), tmp_path / "out.pptx", theme="navy", planner=CrammingPlanner(),
           before_attempt=lambda number, agent: seen.append((number, agent.model)))
    assert seen == [(1, "claude-sonnet-5"), (2, "claude-sonnet-5")]


def test_a_repair_outage_keeps_the_verified_deck(tmp_path):
    out = tmp_path / "out.pptx"
    result = polish(_long_deck(tmp_path), out, theme="navy", planner=CrammingPlanner(repair="error"))
    assert result.ok and out.exists() and result.attempts[1].error == "refusal"
    assert result.qa_report["fallback_slides"]


def test_rerender_reports_qa(tmp_path, deck_path):
    first = polish(deck_path, tmp_path / "navy.pptx", theme="navy", planner=HeuristicPlanner())
    result = rerender(deck_path, tmp_path / "slate.pptx", "slate", first.plan)
    assert result.ok and result.qa is not None and result.overflow_slides == []
