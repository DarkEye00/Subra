"""Stage 6b: a vision model scores rendered slides (17 Oct build plan), behind a flag.

Runs only on slides that passed the deterministic checks, one call per slide
with the previous slide alongside for consistency. A slide scoring under 4 gets
a ``vision`` fault and goes through the same repair loop as any other fault.
Outages never fail the job: the deck was already verified.
"""

import time
from dataclasses import dataclass
from pathlib import Path

from .ai import ProviderError, Usage
from .cost import cost_usd
from .qa import Fault

VISION_THRESHOLD = 4.0
DEFAULT_MODEL = "claude-haiku-5-5"
CRITERIA = ("alignment", "balance", "readability", "consistency")

VISION_PROMPT = (
    "You review a rendered slide image for design quality. Score 1 to 5 on alignment, balance, readability, "
    "and consistency with the previous slide (also provided, when there is one). List faults as "
    '{"shape_hint", "problem"} using plain words: overflow, overlap, cramped, empty space, tiny text, '
    "misaligned, clashing colour. Do not comment on wording or content. Return only JSON matching the schema."
)

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        **{c: {"type": "integer"} for c in CRITERIA},
        "faults": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"shape_hint": {"type": "string"}, "problem": {"type": "string"}},
                "required": ["shape_hint", "problem"],
                "additionalProperties": False,
            },
        },
    },
    "required": [*CRITERIA, "faults"],
    "additionalProperties": False,
}


@dataclass
class SlideReview:
    index: int
    score: float  # mean of the four criteria
    faults: list  # "shape_hint: problem"


@dataclass
class VisionAttempt:
    reviews: dict | None  # slide index -> SlideReview; None when nothing was reviewed
    model: str | None
    usage: Usage
    cost_usd: float
    duration_ms: int
    error: str | None = None
    retryable: bool = False


class VisionQA:
    name = "claude"

    def __init__(self, provider, model=DEFAULT_MODEL, effort="low", pricing=None):
        self.provider = provider
        self.model = model
        self.effort = effort
        self.pricing = pricing

    def review(self, pngs, indexes):
        """Review the slides at ``indexes`` (1-based into ``pngs``). Stops at the first provider error."""
        started = time.monotonic()
        pngs = [Path(p) for p in pngs]
        usage = Usage(0, 0)
        reviews = {}
        error, retryable = None, False
        for idx in indexes:
            images = [pngs[idx - 2].read_bytes()] if idx > 1 else []
            images.append(pngs[idx - 1].read_bytes())
            user = f"Slide {idx} of {len(pngs)}. " + (
                "The first image is the previous slide; the second is the slide to review."
                if idx > 1 else "This is the first slide; review it on its own."
            )
            try:
                result = self.provider.complete_json(
                    model=self.model, system=VISION_PROMPT, user=user, schema=VISION_SCHEMA, effort=self.effort,
                    max_tokens=2000, images=images,
                )
            except ProviderError as e:
                if e.usage:
                    usage = _add(usage, e.usage)
                error, retryable = f"Vision QA request failed: {e}", e.retryable
                break
            usage = _add(usage, result.usage)
            data = result.data or {}
            scores = [int(data.get(c, 0)) for c in CRITERIA]
            faults = [f"{f.get('shape_hint', '')}: {f.get('problem', '')}".strip(": ")
                      for f in data.get("faults") or [] if isinstance(f, dict)]
            reviews[idx] = SlideReview(idx, sum(scores) / len(scores), faults)
        return VisionAttempt(
            reviews=reviews or None, model=self.model, usage=usage, cost_usd=cost_usd(self.model, usage, self.pricing),
            duration_ms=int((time.monotonic() - started) * 1000), error=error, retryable=retryable,
        )


def _add(a, b):
    return Usage(a.input_tokens + b.input_tokens, a.output_tokens + b.output_tokens,
                 a.cache_read_tokens + b.cache_read_tokens, a.cache_write_tokens + b.cache_write_tokens)


def review_deck(run, state, vision, planner, labels, preview_dir, preview_fn, max_passes):
    """Render previews, review the clean slides, send low scores through one repair pass, and review what
    changed. Returns the preview PNGs of the delivered deck."""
    from .pipeline import _timed
    from .preview import render_previews

    if preview_dir is None:
        raise ValueError("Vision QA needs a preview_dir to render slide images into")
    preview_fn = preview_fn or render_previews
    pngs = _previews(run, preview_dir, preview_fn)
    if len(pngs) < len(state.qa.slides):  # LibreOffice failed; the deck is verified anyway, so skip the review
        return pngs
    attempt = _review(run, vision, pngs, [s.index for s in state.qa.slides if s.ok])
    if not attempt.reviews:
        return pngs
    _apply(state.qa, attempt.reviews)
    before = state.qa
    with _timed(run.timings, "vision_repair"):
        run.repair_loop(state, planner, labels, max(0, max_passes - run.repair_passes))
    if state.qa is before:
        return pngs
    pngs = _previews(run, preview_dir, preview_fn)
    changed = run.attempted_sources | run.fallback_sources
    again = _review(run, vision, pngs, [s.index for s in state.qa.slides if s.source_slide in changed and s.ok])
    if again.reviews:
        _apply(state.qa, again.reviews)
    return pngs


def _previews(run, preview_dir, preview_fn):
    from .pipeline import _timed

    with _timed(run.timings, "previews"):
        return list(preview_fn(run.output_path, preview_dir))


def _review(run, vision, pngs, indexes):
    from .pipeline import AttemptRecord, _timed

    with _timed(run.timings, "vision"):
        number = run._start(vision)
        attempt = vision.review(pngs, indexes) if indexes else VisionAttempt(None, vision.model, Usage(0, 0), 0.0, 0)
        run._finish(AttemptRecord(
            number=number, planner=vision.name, model=attempt.model, usage=attempt.usage, cost_usd=attempt.cost_usd,
            duration_ms=attempt.duration_ms, stage="vision", error=attempt.error, retryable=False,
        ))
    return attempt


def _apply(qa, reviews):
    for s in qa.slides:
        r = reviews.get(s.index)
        if r is None:
            continue
        s.vision_score = r.score
        s.faults = [f for f in s.faults if f.kind != "vision"]
        if r.score < VISION_THRESHOLD:
            s.faults.append(Fault("vision", "; ".join(r.faults) or f"design score {r.score:.1f} of 5"))
