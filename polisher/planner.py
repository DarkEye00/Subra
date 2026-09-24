"""Step 3: plan the redesign.

``ClaudePlanner`` sends the deck's text and structure (never image bytes) to
Claude and gets back a layout plan as structured output (JSON schema), which
works on both the planner and escalation models. ``HeuristicPlanner`` is the
free offline stand-in with the same interface.
"""

import json
import time
from dataclasses import dataclass

from .ai import ProviderError, Usage
from .cost import cost_usd
from .heuristic import plan_deck
from .layouts import LAYOUTS
from .plan import PlanFormatError, _plannable, parse_plan

MAX_FEEDBACK_LINES = 60
MAX_CATEGORIES = 12
MAX_CELL_CHARS = 40


@dataclass
class PlanAttempt:
    planner: str  # "claude" | "heuristic"
    model: str | None
    plan: object  # Plan, or None when the attempt failed
    usage: Usage
    cost_usd: float
    duration_ms: int
    raw: dict | None = None
    error: str | None = None
    retryable: bool = False


def _layout_library():
    lines = []
    for layout in LAYOUTS.values():
        lines.append(f"### {layout.name}\n{layout.description}")
        for slot in layout.slots:
            kind = {"text": "text IDs", "visual": "table/chart/image IDs", "stat": "stat entries"}[slot.accepts]
            limits = []
            if slot.min_items:
                limits.append(f"at least {slot.min_items}")
            if slot.max_items is not None:
                limits.append(f"at most {slot.max_items}")
            limit = f" ({', '.join(limits)})" if limits else ""
            lines.append(f"- `{slot.name}`: {kind}{limit}. {slot.description}")
        lines.append("")
    return "\n".join(lines)


SYSTEM_PROMPT = f"""You are the layout designer inside SUBRA, a service that turns a customer's PowerPoint deck into a polished, fully editable deck in one of our themes. Customers are Kenyan professionals and businesses; their decks are investor updates, board papers, training material, sales pitches, and reports.

You receive the deck's content as JSON. Every paragraph, table, chart, and image has an ID. Your job is to choose a layout for every slide and place each content ID into that layout's slots. Our renderer then draws the slides, copying words and numbers from the original by ID, so you never write slide text yourself.

## Hard rules

Code checks these after you answer, and rejects a plan that breaks them.

1. Every content ID in the input appears in your plan exactly once. No omissions, no duplicates, no invented IDs.
2. Never write, rewrite, shorten, translate, or summarise text. You only reference IDs. The one place you type text is a stat highlight (see below), and it must be copied exactly from the start of the paragraph.
3. Slide order stays the same. `source_slide` never decreases from one planned slide to the next, and every source slide in the input appears at least once.
4. An item may only be placed on a planned slide whose `source_slide` is the slide the item came from.
5. You may split one overcrowded source slide into several planned slides with the same `source_slide`, as long as every item still appears exactly once. Titles can't be repeated, so only the first part of a split has a title.
6. Use only the layouts and slots listed below. Text IDs go only in text slots, tables, charts, images and locked objects only in `visuals`, and stat entries only in `stats`.
7. Locked objects (type "locked": video, audio, embedded or linked files, equations, 3D models, SmartArt, ink, and groups that contain them) are kept exactly as they are. Put each one in a `visuals` slot on the first planned slide for its source slide. You can't remove or split them; the renderer only moves and scales them.
8. Images can only be placed and scaled. You can't see inside them, so treat them as opaque.
9. Picture backgrounds (type "background") are replaced by the theme. If one carries content (a photo, map, chart or text), put it in a `visuals` slot and it stays as a normal picture. If it's decorative, leave it out. Backgrounds are the only items you may leave out.

## What you may change

- The layout of each slide.
- The grouping and order of items within a slide, when that makes the slide read better. Keep the original reading order unless there is a clear reason.
- Emphasis: turning key numbers into stat cards, putting short column headings into `left_title`/`right_title`, giving a single strong statement the `quote` layout.
- Splitting an overcrowded slide.

## Layouts

{_layout_library()}
## Stat entries

Each stat entry is {{"ids": [...], "highlight": string or null}}.
- The first ID's paragraph is shown large; any further IDs are shown as the card's label.
- If the first paragraph mixes a figure with words ("45% growth in repeat orders"), set `highlight` to the exact leading figure to show large ("45%"); the rest of that paragraph becomes the label. The highlight must be copied character for character from the very start of the paragraph and must end at a word boundary (followed by a space or the end of the text). Otherwise use null.
- Use stats when the figures are the point of the slide: 1 to 4 cards.

## Design guidance

- Slide 1 is almost always `title`. A slide that only announces the next part of the deck is a `section`. A closing "Thank you" or "Questions" slide with contact details works well as `title` or `quote`.
- Default to `bullets` for text. Prefer `two_column` when the original had two text boxes side by side (see `boxes`), or when the text is a comparison (pros and cons, before and after, two options). If a column starts with a short heading paragraph, put it in `left_title`/`right_title`.
- Tables go in `table`, charts in `chart`, photos, videos and other locked objects in `image_text` (or `chart` when they sit beside a chart). Put a slide's explanatory text in that slide's `body`.
- The renderer shrinks text to fit, down to about 12pt, and flows long lists of short lines into two columns by itself. Split a slide when its text is long: more than about 14 lines, more than about 170 words, or several distinct groups of points. Split at a top-level paragraph so sub-points stay with their parent.
- Keep a paragraph's sub-points (higher `level`) in the same slot as their parent.
- Slides with no content are not sent to you; ignore them.
- Aim for variety and rhythm across the deck, but never at the cost of the rules.

## Input format

{{"slides": [{{"slide": n, "items": [...], "boxes": {{box_id: [x, y, w, h]}}, "hidden": true (only when hidden)}}]}}

Items:
- text: {{"id", "type": "text", "role": "title" | "subtitle" | "body", "text", "level" (0 = top level; omitted when 0), "bullet": "char" | "number" (omitted when none), "box": ID shared by paragraphs from the same original text box}}
- table: {{"id", "type": "table", "rows", "cols", "header": first row, "sample": the next rows (truncated)}}
- chart: {{"id", "type": "chart", "chart_type", "title", "categories" (may be truncated), "series": names}}
- image: {{"id", "type": "image", "aspect": width / height, "alt": alt text, "pos": [x, y, w, h]}}
- locked: {{"id", "type": "locked", "object": "video" | "audio" | "ole" | "equation" | "model3d" | "smartart" | "ink" | "group" | ..., "aspect"}}
- background: {{"id", "type": "background", "aspect"}}

Positions are fractions of the slide width and height, measured from the top-left corner.

If the request lists problems with a previous plan, fix every one of them.

## Output

Return JSON matching the schema: {{"slides": [{{"source_slide", "layout", "slots": [{{"name", "ids"}}], "stats": [stat entries]}}]}}.
List only the slots you use, each slot name at most once per slide. `stats` is [] unless the layout is `stats`.

## Example

Input slide:
{{"slide": 4, "items": [
  {{"id": "s4-t1", "type": "text", "role": "title", "text": "2026 at a glance", "box": "s4-b1"}},
  {{"id": "s4-t2", "type": "text", "role": "body", "text": "KES 450m revenue", "bullet": "char", "box": "s4-b2"}},
  {{"id": "s4-t3", "type": "text", "role": "body", "text": "38% gross margin", "bullet": "char", "box": "s4-b2"}},
  {{"id": "s4-t4", "type": "text", "role": "body", "text": "Figures are unaudited", "bullet": "char", "box": "s4-b2"}}
], "boxes": {{"s4-b1": [0.05, 0.04, 0.9, 0.17], "s4-b2": [0.05, 0.24, 0.9, 0.66]}}}}

A good planned slide:
{{"source_slide": 4, "layout": "stats",
  "slots": [{{"name": "title", "ids": ["s4-t1"]}}, {{"name": "body", "ids": ["s4-t4"]}}],
  "stats": [{{"ids": ["s4-t2"], "highlight": "KES 450m"}}, {{"ids": ["s4-t3"], "highlight": "38%"}}]}}
"""

_STAT_ENTRY = {
    "type": "object",
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"}},
        "highlight": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    },
    "required": ["ids", "highlight"],
    "additionalProperties": False,
}
_SLOT_NAMES = sorted({s.name for layout in LAYOUTS.values() for s in layout.slots if s.accepts != "stat"})

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source_slide": {"type": "integer"},
                    "layout": {"type": "string", "enum": list(LAYOUTS)},
                    "slots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "enum": _SLOT_NAMES},
                                "ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["name", "ids"],
                            "additionalProperties": False,
                        },
                    },
                    "stats": {"type": "array", "items": _STAT_ENTRY},
                },
                "required": ["source_slide", "layout", "slots", "stats"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["slides"],
    "additionalProperties": False,
}


def _pos(p):
    return [round(v, 2) for v in p]


def _cell(text):
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_CELL_CHARS else text[: MAX_CELL_CHARS - 1] + "…"


def planner_input(deck):
    """The deck's text and structure for the planner: every plannable ID, no binaries."""
    plannable = _plannable(deck)
    slides = []
    for s in deck.slides:
        items = []
        boxes = {}
        for item_id in s.order:
            item = plannable.get(item_id)
            if item is None:
                continue
            if item.kind == "text":
                entry = {"id": item.id, "type": "text", "role": item.role, "text": item.text}
                if item.level:
                    entry["level"] = item.level
                if item.bullet:
                    entry["bullet"] = item.bullet
                entry["box"] = item.box
                boxes[item.box] = _pos(s.box_pos[item.box])
            elif item.kind == "table":
                visible = [[_cell("" if c.spanned else c.text) for c in row] for row in item.cells]
                entry = {"id": item.id, "type": "table", "rows": item.rows, "cols": item.cols,
                         "header": visible[0], "sample": visible[1:3]}
            elif item.kind == "chart":
                entry = {"id": item.id, "type": "chart", "chart_type": item.chart_type}
                if item.title:
                    entry["title"] = item.title
                entry["categories"] = [_cell(c) for c in item.categories[:MAX_CATEGORIES]]
                entry["series"] = [s_.name for s_ in item.series]
            elif item.kind == "locked":
                entry = {"id": item.id, "type": "locked", "object": item.lock_type, "aspect": round(item.aspect, 2)}
            elif item.kind == "background":
                entry = {"id": item.id, "type": "background", "aspect": round(item.aspect, 2)}
            else:
                entry = {"id": item.id, "type": "image", "aspect": round(item.display_aspect, 2)}
                if item.alt_text:
                    entry["alt"] = _cell(item.alt_text)
                entry["pos"] = _pos(item.pos)
            items.append(entry)
        if not items:
            continue
        slide = {"slide": s.number, "items": items}
        if boxes:
            slide["boxes"] = boxes
        if s.hidden:
            slide["hidden"] = True
        slides.append(slide)
    return {"slides": slides}


def _user_message(deck, feedback):
    content = json.dumps(planner_input(deck), ensure_ascii=False, separators=(",", ":"))
    msg = f"Plan the layout for this deck.\n\n<deck>\n{content}\n</deck>"
    if feedback:
        lines = list(feedback)[:MAX_FEEDBACK_LINES]
        more = len(feedback) - len(lines)
        problems = "\n".join(f"- {p}" for p in lines) + (f"\n- ...and {more} more" if more > 0 else "")
        msg += (
            "\n\nA previous plan for this deck was rejected. Fix every problem below "
            f"and return a complete new plan.\n<problems>\n{problems}\n</problems>"
        )
    return msg


def structured_to_plan_dict(data):
    """Convert Claude's structured output (slots as a list) into the plan dict format."""
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise PlanFormatError("Output has no 'slides' list.")
    slides = []
    for s in data["slides"]:
        slots = {}
        for slot in s.get("slots") or []:
            slots.setdefault(slot["name"], []).extend(slot.get("ids") or [])
        if s.get("stats"):
            slots["stats"] = [{"ids": e.get("ids") or [], "highlight": e.get("highlight")} for e in s["stats"]]
        slides.append({"source_slide": s.get("source_slide"), "layout": s.get("layout"), "slots": slots})
    return {"slides": slides}


class ClaudePlanner:
    name = "claude"

    def __init__(self, provider, model, effort="medium", pricing=None):
        self.provider = provider
        self.model = model
        self.effort = effort
        self.pricing = pricing

    def plan(self, deck, feedback=None):
        started = time.monotonic()
        try:
            result = self.provider.complete_json(
                model=self.model,
                system=SYSTEM_PROMPT,
                user=_user_message(deck, feedback),
                schema=PLAN_SCHEMA,
                effort=self.effort,
            )
        except ProviderError as e:
            usage = e.usage or Usage(0, 0)
            return PlanAttempt(
                planner=self.name, model=self.model, plan=None, usage=usage,
                cost_usd=cost_usd(self.model, usage, self.pricing),
                duration_ms=int((time.monotonic() - started) * 1000),
                error=f"Planner request failed: {e}", retryable=e.retryable,
            )
        cost = cost_usd(self.model, result.usage, self.pricing)
        try:
            plan = parse_plan(structured_to_plan_dict(result.data))
            error = None
        except (PlanFormatError, KeyError, TypeError, AttributeError) as e:
            plan, error = None, f"Planner output was not a usable plan: {e}"
        return PlanAttempt(
            planner=self.name, model=self.model, plan=plan, usage=result.usage, cost_usd=cost,
            duration_ms=result.duration_ms, raw=result.data, error=error,
        )


class HeuristicPlanner:
    """Rule-based planner: no API calls, no cost. For offline runs and tests."""

    name = "heuristic"
    model = None

    def plan(self, deck, feedback=None):
        started = time.monotonic()
        plan = plan_deck(deck)
        return PlanAttempt(
            planner=self.name, model=None, plan=plan, usage=Usage(0, 0), cost_usd=0.0,
            duration_ms=int((time.monotonic() - started) * 1000), raw=plan.to_dict(),
        )
