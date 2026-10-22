"""Stage 2: label each slide with a type and a one-sentence intent (17 Oct build plan).

The labels are hints for the planner, never instructions; the pipeline runs
without them when the classifier fails. ``ClaudeClassifier`` makes one cheap
call per deck; ``HeuristicClassifier`` is the free offline stand-in.
"""

import json
import time
from dataclasses import dataclass

from .ai import ProviderError, Usage
from .cost import cost_usd
from .heuristic import AGENDA_TITLE, _stat_entries, _two_boxes
from .plan import _plannable

SLIDE_TYPES = ("title", "section", "agenda", "bullets", "statement", "kpi", "comparison", "table", "chart",
               "image-led", "timeline", "quote", "closing", "mixed")
DEFAULT_MODEL = "claude-haiku-5-5"
MAX_PARAGRAPHS = 12
MAX_CHARS = 120
MAX_CLOSING_CHARS = 120

CLASSIFIER_PROMPT = (
    "You classify PowerPoint slides. For each slide in the JSON array, return "
    '{"slide", "type", "intent"} where type is one of: ' + ", ".join(SLIDE_TYPES) + ". "
    "intent is one sentence on what the slide is trying to say.\n"
    "Return only JSON matching the schema. Never drop a slide."
)

CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slide": {"type": "integer"},
                    "type": {"type": "string", "enum": list(SLIDE_TYPES)},
                    "intent": {"type": "string"},
                },
                "required": ["slide", "type", "intent"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["slides"],
    "additionalProperties": False,
}


@dataclass
class SlideLabel:
    type: str
    intent: str


@dataclass
class ClassifyAttempt:
    labels: dict | None  # slide number -> SlideLabel; None when the call failed
    model: str | None
    usage: Usage
    cost_usd: float
    duration_ms: int
    error: str | None = None
    retryable: bool = False


def _cut(text):
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "…"


def classifier_input(deck):
    """One small entry per slide: title, the first paragraphs (cut short), and counts of visuals."""
    out = []
    for s in deck.slides:
        titles = [t.text for t in s.texts if t.role == "title"]
        body = [t.text for t in s.texts if t.role != "title" and t.text.strip()]
        out.append({
            "slide": s.number,
            "title": _cut(" ".join(titles)),
            "text": [_cut(t) for t in body[:MAX_PARAGRAPHS]],
            "tables": len(s.tables),
            "charts": len(s.charts),
            "images": len(s.images) + len(s.backgrounds),
            "locked": len(s.locked),
        })
    return out


class ClaudeClassifier:
    name = "claude"

    def __init__(self, provider, model=DEFAULT_MODEL, effort="low", pricing=None):
        self.provider = provider
        self.model = model
        self.effort = effort
        self.pricing = pricing

    def classify(self, deck):
        started = time.monotonic()
        user = json.dumps(classifier_input(deck), ensure_ascii=False)
        try:
            result = self.provider.complete_json(
                model=self.model, system=CLASSIFIER_PROMPT, user=user, schema=CLASSIFY_SCHEMA, effort=self.effort,
                max_tokens=16000,
            )
        except ProviderError as e:
            usage = e.usage or Usage(0, 0)
            return ClassifyAttempt(
                labels=None, model=self.model, usage=usage, cost_usd=cost_usd(self.model, usage, self.pricing),
                duration_ms=int((time.monotonic() - started) * 1000), error=f"Classifier request failed: {e}",
                retryable=e.retryable,
            )
        numbers = {s.number for s in deck.slides}
        labels = {}
        for entry in (result.data or {}).get("slides") or []:
            if not isinstance(entry, dict):
                continue
            n, kind = entry.get("slide"), entry.get("type")
            if n in numbers and kind in SLIDE_TYPES:
                labels[n] = SlideLabel(kind, str(entry.get("intent") or ""))
        return ClassifyAttempt(
            labels=labels, model=self.model, usage=result.usage,
            cost_usd=cost_usd(self.model, result.usage, self.pricing), duration_ms=result.duration_ms,
        )


class HeuristicClassifier:
    """Rule-based labels: no API calls, no cost. Intent is left empty."""

    name = "heuristic"
    model = None

    def classify(self, deck):
        started = time.monotonic()
        items = _plannable(deck)
        last = deck.slides[-1].number if deck.slides else 0
        labels = {}
        for s in deck.slides:
            own = [items[i] for i in s.order if i in items]
            labels[s.number] = SlideLabel(_label(s, own, s.number == 1, s.number == last), "")
        return ClassifyAttempt(labels=labels, model=None, usage=Usage(0, 0), cost_usd=0.0,
                               duration_ms=int((time.monotonic() - started) * 1000))


def _label(slide, items, first, last):
    texts = [i for i in items if i.kind == "text"]
    titles = [t for t in texts if t.role == "title"]
    body = [t for t in texts if t.role not in ("title", "subtitle")]
    if first:
        return "title"
    if slide.tables:
        return "table"
    if slide.charts:
        return "chart"
    if slide.images or slide.locked or slide.backgrounds:
        return "image-led"
    total_chars = sum(len(t.text) for t in texts)
    if last and total_chars <= MAX_CLOSING_CHARS and len(texts) <= 3:
        return "closing"
    if not texts:
        return "mixed"
    if not body:
        return "section"
    if titles and AGENDA_TITLE.search(" ".join(t.text for t in titles)) and 3 <= len(body) <= 7:
        return "agenda"
    if _stat_entries(body):
        return "kpi"
    if _two_boxes(body):
        return "comparison"
    return "bullets"
