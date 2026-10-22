"""The full polish run: validate -> extract -> (classify) -> plan -> render -> verify -> check -> repair.

Retry policy (build plan, section 6): if a plan is unusable or the rendered deck
fails verification, plan again once with the list of problems; if that fails
too, escalate to the stronger model once. If that fails, the run fails (and the
customer isn't charged). API outages raise PlannerUnavailable instead, so the job
can be retried later without counting against the customer.

Repair loop (17 Oct build plan, stages 6 and 7): once a deck is verified, the
rendered file is checked for layout faults. Slides with faults re-planning can
fix go back to the planner, at most twice; a slide still failing gets the
plainest safe layout and is flagged in the QA report. A repair is only kept when
its deck verifies and has no more faulted slides than before, so the customer
never gets a worse deck than the first verified one.
"""

import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .ai import Usage
from .extract import extract_deck
from .heuristic import fallback_plan
from .plan import PlanFormatError, StatEntry, _plannable, parse_plan, repair_plan, replace_source, validate_plan
from .qa import check_deck
from .render.renderer import render
from .render.themes import get_theme
from .validate import validate_file
from .verify import verify

MAX_REPAIR_PASSES = 2
FALLBACK_LEVEL = 2
STAGES = ("classify", "plan", "repair", "vision")


class UnsupportedContentError(Exception):
    """Content we can't carry over. Locked objects (Amendment 1C) made this rare; kept as a safety net."""

    def __init__(self, problems):
        self.problems = problems
        super().__init__(
            "This deck contains content we can't carry over yet: " + "; ".join(problems)
            + ". Remove it (or replace it with a picture) and upload again."
        )


class PlannerUnavailable(Exception):
    """The AI provider is down or rate-limiting. Retry the whole job later."""

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class AttemptRecord:
    number: int
    planner: str
    model: str | None
    usage: Usage
    cost_usd: float
    duration_ms: int
    stage: str = "plan"  # classify | plan | repair | vision
    verification_passed: bool | None = None
    problems: list = field(default_factory=list)
    repairs: list = field(default_factory=list)
    error: str | None = None
    retryable: bool = False  # the provider was unavailable; worth retrying the whole job later
    plan: dict | None = None


@dataclass
class PolishResult:
    ok: bool
    output_path: Path | None
    slide_count: int
    attempts: list
    render_report: object = None
    verification: object = None
    failure: str | None = None
    qa: object = None  # polisher.qa.QAReport
    final_plan: dict | None = None
    labels: dict | None = None  # slide number -> SlideLabel
    timings_ms: dict = field(default_factory=dict)
    previews: list = field(default_factory=list)
    repair_passes: int = 0
    vision_ran: bool = False

    @property
    def total_cost_usd(self):
        return sum(a.cost_usd for a in self.attempts)

    @property
    def overflow_slides(self):
        """Rendered slide indexes with a remaining layout fault (kept under its old name for the web app)."""
        return self.qa.flagged_slides if self.qa else []

    @property
    def plan(self):
        """The verified plan the delivered deck was rendered from, saved so a theme switch can re-render it."""
        return self.final_plan

    @property
    def qa_report(self):
        """The short QA report delivered with the deck: retention, repairs, fallbacks, cost and time."""
        counts = self.verification.counts if self.verification else {}
        slides = self.qa.slides if self.qa else []
        cost = {stage: sum(a.cost_usd for a in self.attempts if a.stage == stage) for stage in STAGES}
        cost["total"] = self.total_cost_usd
        return {
            "retention": {"passed": bool(self.verification and self.verification.passed), **counts},
            "slides": [s.to_dict() for s in slides],
            "repaired_slides": [s.index for s in slides if s.repaired],
            "fallback_slides": [s.index for s in slides if s.fallback],
            "flagged_slides": self.overflow_slides,
            "repair_passes": self.repair_passes,
            "vision_qa": self.vision_ran,
            "labels": {str(n): {"type": l.type, "intent": l.intent} for n, l in self.labels.items()}
            if self.labels else None,
            "timings_ms": self.timings_ms,
            "cost_usd": cost,
        }


@contextmanager
def _timed(timings, key):
    started = time.monotonic()
    try:
        yield
    finally:
        timings[key] = timings.get(key, 0) + int((time.monotonic() - started) * 1000)


def polish(input_path, output_path, theme, planner, escalation=None, render_fn=render, before_attempt=None,
           on_attempt=None, classifier=None, vision=None, preview_dir=None, preview_fn=None,
           max_repair_passes=MAX_REPAIR_PASSES, abort_on=()):
    """Run the whole pipeline. ``before_attempt(number, agent)`` runs before each AI call (the
    spend cap hooks in here); ``on_attempt(record)`` runs as soon as each attempt finishes.
    Exceptions of the ``abort_on`` types (a worker's time limit) escape instead of counting as a failed render."""
    input_path, output_path = Path(input_path), Path(output_path)
    timings = {}
    started = time.monotonic()
    with _timed(timings, "extract"):
        info = validate_file(input_path)
        deck = extract_deck(input_path)
    if deck.unsupported:
        raise UnsupportedContentError(deck.unsupported)
    theme_obj = get_theme(theme)
    run = _Run(deck, theme_obj, input_path, output_path, render_fn, before_attempt, on_attempt, timings, abort_on)

    labels = None
    if classifier is not None:
        labels = run.classify(classifier)

    schedule = [planner, planner] + ([escalation] if escalation is not None else [])
    feedback = None
    verified = None
    for current in schedule:
        with _timed(timings, "plan"):
            record, feedback, verified = run.attempt(current, feedback, labels)
        if record.retryable:
            raise PlannerUnavailable(record.error, run.attempts)
        if verified is not None:
            break

    if verified is None:
        timings["total"] = int((time.monotonic() - started) * 1000)
        return PolishResult(
            ok=False, output_path=None, slide_count=info.slide_count, attempts=run.attempts, labels=labels,
            timings_ms=timings, failure=f"No plan passed verification after {len(run.attempts)} attempts",
        )

    plan, report, verification = verified
    with _timed(timings, "qa"):
        qa = check_deck(output_path, report, theme_obj)
    state = _Verified(plan, report, verification, qa)
    run.repair_loop(state, planner, labels, max_repair_passes)

    previews = []
    if vision is not None:
        previews = run.vision_pass(state, vision, planner, labels, preview_dir, preview_fn, max_repair_passes)

    state.qa.mark(repaired_sources=run.repaired_sources(state.qa), fallback_sources=run.fallback_sources)
    timings["total"] = int((time.monotonic() - started) * 1000)
    return PolishResult(
        ok=True, output_path=output_path, slide_count=info.slide_count, attempts=run.attempts,
        render_report=state.report, verification=state.verification, qa=state.qa, final_plan=state.plan.to_dict(),
        labels=labels, timings_ms=timings, previews=previews, repair_passes=run.repair_passes,
        vision_ran=vision is not None,
    )


@dataclass
class _Verified:
    """The best deck so far: its plan, render report, verification and QA. Only replaced by a better one."""

    plan: object
    report: object
    verification: object
    qa: object


class _Run:
    """State shared by the stages of one polish run."""

    def __init__(self, deck, theme, input_path, output_path, render_fn, before_attempt, on_attempt, timings,
                 abort_on=()):
        self.deck, self.theme = deck, theme
        self.abort_on = tuple(abort_on)
        self.input_path, self.output_path = input_path, output_path
        self.render_fn = render_fn
        self.before_attempt, self.on_attempt = before_attempt, on_attempt
        self.timings = timings
        self.attempts = []
        self.repair_passes = 0
        self.attempted_sources = set()  # sources ever sent back to the planner
        self.fallback_sources = set()

    # --- bookkeeping --------------------------------------------------------

    def _start(self, agent):
        number = len(self.attempts) + 1
        if self.before_attempt is not None:
            self.before_attempt(number, agent)
        return number

    def _finish(self, record):
        self.attempts.append(record)
        if self.on_attempt is not None:
            self.on_attempt(record)
        return record

    def repaired_sources(self, qa):
        still_faulted = {s.source_slide for s in qa.faulted}
        return self.attempted_sources - still_faulted - self.fallback_sources

    # --- stages ----------------------------------------------------------------

    def classify(self, classifier):
        with _timed(self.timings, "classify"):
            number = self._start(classifier)
            attempt = classifier.classify(self.deck)
            self._finish(AttemptRecord(
                number=number, planner=classifier.name, model=attempt.model, usage=attempt.usage,
                cost_usd=attempt.cost_usd, duration_ms=attempt.duration_ms, stage="classify", error=attempt.error,
            ))
        return attempt.labels

    def attempt(self, planner, feedback, labels):
        """One plan -> repair -> render -> verify pass. Returns (record, next_feedback, verified)."""
        number = self._start(planner)
        attempt = planner.plan(self.deck, feedback=feedback, labels=labels)
        record = AttemptRecord(
            number=number, planner=attempt.planner, model=attempt.model, usage=attempt.usage,
            cost_usd=attempt.cost_usd, duration_ms=attempt.duration_ms, error=attempt.error,
        )
        if attempt.plan is None:
            record.retryable = attempt.retryable
            self._finish(record)
            return record, [attempt.error or "The previous answer was not a usable plan."], None

        plan, repairs = repair_plan(attempt.plan, self.deck)
        record.repairs = repairs
        record.plan = plan.to_dict()
        errors = validate_plan(plan, self.deck)
        if errors:
            record.error = "Plan broke the rules after repair"
            record.problems = errors
            self._finish(record)
            return record, errors, None

        outcome = self._render_verified(plan)
        if outcome.error:
            record.error = outcome.error
            self._finish(record)
            return record, [
                f"Rendering this plan crashed ({outcome.error_type}). Try a different layout for the affected slides."
            ], None
        record.verification_passed = outcome.verification.passed
        record.problems = outcome.verification.problems
        if not outcome.verification.passed:
            outcome.discard()
            self._finish(record)
            return record, outcome.verification.problems, None
        outcome.keep(self.output_path)
        self._finish(record)
        return record, None, (plan, outcome.report, outcome.verification)

    def repair_loop(self, state, planner, labels, max_passes):
        """Send faulted slides back to the planner, at most ``max_passes`` times, then fall back."""
        passes = 0
        sources = self._repairable(state)
        while sources and passes < max_passes:
            passes += 1
            self.repair_passes += 1
            with _timed(self.timings, "repair"):
                number = self._start(planner)
                attempt = planner.repair(self.deck, state.plan, state.qa.faults_by_source(), labels=labels,
                                         attempt=self.repair_passes)
                self.attempted_sources.update(sources)
                record = AttemptRecord(
                    number=number, planner=attempt.planner, model=attempt.model, usage=attempt.usage,
                    cost_usd=attempt.cost_usd, duration_ms=attempt.duration_ms, stage="repair",
                    problems=[line for n in sources for line in state.qa.faults_by_source().get(n, [])],
                    error=attempt.error,
                )
                if attempt.plan is None:
                    self._finish(record)
                    break
                replacements = {}
                for ps in attempt.plan.slides:
                    if ps.source_slide in sources:
                        replacements.setdefault(ps.source_slide, []).append(ps)
                accepted = self._try_replacements(state, replacements, record)
                self._finish(record)
                if not accepted:
                    break
            sources = self._repairable(state)

        if sources:
            with _timed(self.timings, "repair"):
                items = _plannable(self.deck)
                replacements = {}
                for n in sources:
                    own = [items[i] for i in self.deck.slides[n - 1].order if i in items]
                    replacements[n] = fallback_plan(n, own, level=FALLBACK_LEVEL)
                if self._try_replacements(state, replacements, None):
                    self.fallback_sources.update(sources)

    def vision_pass(self, state, vision, planner, labels, preview_dir, preview_fn, max_passes):
        from .vision import review_deck

        return review_deck(self, state, vision, planner, labels, preview_dir, preview_fn, max_passes)

    # --- helpers ------------------------------------------------------------------

    def _repairable(self, state):
        """Faulted source slides whose plan holds more than one item besides the title: re-planning can't
        help a lone table or chart, however it is titled."""
        counts = {}
        for ps in state.plan.slides:
            counts[ps.source_slide] = counts.get(ps.source_slide, 0) + _items_besides_title(ps)
        return [n for n in state.qa.repairable_sources if counts.get(n, 0) >= 2]

    def _try_replacements(self, state, replacements, record):
        """Splice replacement parts into the plan, render, verify and check. Keep the result only if it
        verifies and is no worse. Returns True when the state was replaced."""
        candidate = state.plan
        for n, parts in replacements.items():
            candidate = replace_source(candidate, n, parts)
        candidate, repairs = repair_plan(candidate, self.deck)
        if candidate.to_dict() == state.plan.to_dict():  # the same plan again would render the same faults
            return False
        errors = validate_plan(candidate, self.deck)
        if record is not None:
            record.repairs = repairs
        if errors:
            if record is not None:
                record.error = "Repair broke the rules after repair"
                record.problems = errors
            return False
        outcome = self._render_verified(candidate)
        if outcome.error:
            if record is not None:
                record.error = outcome.error
            return False
        if record is not None:
            record.verification_passed = outcome.verification.passed
        if not outcome.verification.passed:
            if record is not None:
                record.problems = outcome.verification.problems
            outcome.discard()
            return False
        with _timed(self.timings, "qa"):
            qa = check_deck(outcome.path, outcome.report, self.theme)
        if len(qa.faulted) > len(state.qa.faulted):
            outcome.discard()
            return False
        outcome.keep(self.output_path)
        state.plan, state.report, state.verification, state.qa = candidate, outcome.report, outcome.verification, qa
        if record is not None:
            record.plan = candidate.to_dict()
        return True

    def _render_verified(self, plan):
        """Render a plan to a temporary file next to the output and verify it."""
        fd, tmp_name = tempfile.mkstemp(suffix=".pptx", dir=self.output_path.parent)
        os.close(fd)
        outcome = _Outcome(Path(tmp_name))
        try:
            outcome.report = self.render_fn(plan, self.deck, self.theme, outcome.path, self.input_path)
            outcome.verification = verify(self.deck, extract_deck(outcome.path), slide_sources=outcome.report.slide_sources,
                                          dropped_backgrounds=outcome.report.dropped_backgrounds)
        except Exception as e:  # a renderer crash counts as a failed attempt, not a lost order
            if isinstance(e, self.abort_on):
                outcome.discard()
                raise
            outcome.error_type = type(e).__name__
            outcome.error = f"Render failed: {outcome.error_type}: {e}"
            outcome.discard()
        return outcome


def _items_besides_title(ps):
    n = 0
    for name, entries in ps.slots.items():
        if name == "title":
            continue
        n += sum(len(e.ids) if isinstance(e, StatEntry) else 1 for e in entries)
    return n


@dataclass
class _Outcome:
    path: Path
    report: object = None
    verification: object = None
    error: str | None = None
    error_type: str | None = None

    def keep(self, output_path):
        os.replace(self.path, output_path)

    def discard(self):
        self.path.unlink(missing_ok=True)


@dataclass
class RerenderResult:
    ok: bool
    output_path: Path | None
    render_report: object = None
    verification: object = None
    failure: str | None = None
    qa: object = None

    @property
    def overflow_slides(self):
        if self.qa is not None:
            return self.qa.flagged_slides
        return self.render_report.overflow_slides if self.render_report else []


def rerender(input_path, output_path, theme, plan_dict, render_fn=render, abort_on=()):
    """Render a saved, already-verified plan in another theme: no planner call and no retry.

    A new font can reflow text, so the result is verified and checked again. On failure nothing is written.
    """
    input_path, output_path = Path(input_path), Path(output_path)
    validate_file(input_path)
    deck = extract_deck(input_path)
    theme_obj = get_theme(theme)
    try:
        plan = parse_plan(plan_dict)
    except PlanFormatError as e:
        return RerenderResult(ok=False, output_path=None, failure=f"Saved plan unreadable: {e}")
    errors = validate_plan(plan, deck)
    if errors:
        return RerenderResult(ok=False, output_path=None,
                              failure="Saved plan no longer fits: " + "; ".join(errors[:5]))

    fd, tmp_name = tempfile.mkstemp(suffix=".pptx", dir=output_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        report = render_fn(plan, deck, theme_obj, tmp, input_path)
        verification = verify(deck, extract_deck(tmp), slide_sources=report.slide_sources,
                              dropped_backgrounds=report.dropped_backgrounds)
        if not verification.passed:
            return RerenderResult(ok=False, output_path=None, render_report=report, verification=verification,
                                  failure="Verification failed: " + "; ".join(verification.problems[:5]))
        qa = check_deck(tmp, report, theme_obj)
        os.replace(tmp, output_path)
        return RerenderResult(ok=True, output_path=output_path, render_report=report, verification=verification, qa=qa)
    except Exception as e:  # a crash leaves the previous deck in place
        if isinstance(e, tuple(abort_on)):
            raise
        return RerenderResult(ok=False, output_path=None, failure=f"Render failed: {type(e).__name__}: {e}")
    finally:
        tmp.unlink(missing_ok=True)
