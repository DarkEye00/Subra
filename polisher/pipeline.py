"""The full polish run: validate -> extract -> plan -> render -> verify, with retries.

Retry policy (build plan, section 6): if a plan is unusable or the rendered deck
fails verification, plan again once with the list of problems; if that fails
too, escalate to the stronger model once. If that fails, the run fails and the
order goes to refund. API outages raise PlannerUnavailable instead, so the job
can be retried later without counting against the customer.
"""

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .ai import Usage
from .extract import extract_deck
from .plan import repair_plan, validate_plan
from .render.renderer import render
from .render.themes import get_theme
from .validate import validate_file
from .verify import verify


class UnsupportedContentError(Exception):
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

    @property
    def total_cost_usd(self):
        return sum(a.cost_usd for a in self.attempts)

    @property
    def overflow_slides(self):
        return self.render_report.overflow_slides if self.render_report else []


def polish(input_path, output_path, theme, planner, escalation=None, render_fn=render, before_attempt=None,
           on_attempt=None):
    """Run the whole pipeline. ``before_attempt(number, planner)`` runs before each AI call (the
    spend cap hooks in here); ``on_attempt(record)`` runs as soon as each attempt finishes."""
    input_path, output_path = Path(input_path), Path(output_path)
    info = validate_file(input_path)
    deck = extract_deck(input_path)
    if deck.unsupported:
        raise UnsupportedContentError(deck.unsupported)
    theme_obj = get_theme(theme)

    schedule = [planner, planner] + ([escalation] if escalation is not None else [])
    attempts = []
    feedback = None
    for number, current in enumerate(schedule, start=1):
        if before_attempt is not None:
            before_attempt(number, current)
        record, feedback, success = _attempt(number, current, deck, feedback, theme_obj, output_path, render_fn)
        attempts.append(record)
        if on_attempt is not None:
            on_attempt(record)
        if record.retryable:
            raise PlannerUnavailable(record.error, attempts)
        if success is not None:
            report, verification = success
            return PolishResult(
                ok=True, output_path=output_path, slide_count=info.slide_count, attempts=attempts,
                render_report=report, verification=verification,
            )

    return PolishResult(
        ok=False, output_path=None, slide_count=info.slide_count, attempts=attempts,
        failure=f"No plan passed verification after {len(attempts)} attempts",
    )


def _attempt(number, planner, deck, feedback, theme, output_path, render_fn):
    """One plan -> repair -> render -> verify pass. Returns (record, next_feedback, success)."""
    attempt = planner.plan(deck, feedback=feedback)
    record = AttemptRecord(
        number=number, planner=attempt.planner, model=attempt.model, usage=attempt.usage,
        cost_usd=attempt.cost_usd, duration_ms=attempt.duration_ms, error=attempt.error,
    )
    if attempt.plan is None:
        record.retryable = attempt.retryable
        return record, [attempt.error or "The previous answer was not a usable plan."], None

    plan, repairs = repair_plan(attempt.plan, deck)
    record.repairs = repairs
    record.plan = plan.to_dict()
    errors = validate_plan(plan, deck)
    if errors:
        record.error = "Plan broke the rules after repair"
        record.problems = errors
        return record, errors, None

    fd, tmp_name = tempfile.mkstemp(suffix=".pptx", dir=output_path.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        report = render_fn(plan, deck, theme, tmp)
        verification = verify(deck, extract_deck(tmp))
        record.verification_passed = verification.passed
        record.problems = verification.problems
        if verification.passed:
            os.replace(tmp, output_path)
            return record, None, (report, verification)
        return record, verification.problems, None
    except Exception as e:  # a renderer crash counts as a failed attempt, not a lost order
        record.error = f"Render failed: {type(e).__name__}: {e}"
        return record, [
            f"Rendering this plan crashed ({type(e).__name__}). Try a different layout for the affected slides."
        ], None
    finally:
        tmp.unlink(missing_ok=True)
