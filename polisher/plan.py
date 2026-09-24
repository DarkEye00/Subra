"""The layout plan: which layout each new slide uses and which content IDs go in which slot.

The planner (Claude or the heuristic) produces a plan; ``validate_plan`` checks
the hard rules; ``repair_plan`` fixes what it can deterministically so a
slightly-off plan never costs a retry.
"""

import copy
from collections import Counter
from dataclasses import dataclass, field

from .layouts import BLANK, LAYOUTS
from .verify import normalize_text

TEXT_SLOT_PREFERENCE = ["body", "subtitle", "left", "right", "quote", "attribution"]
VISUAL_LAYOUT_FOR_KIND = {"table": "table", "chart": "chart", "image": "image_text"}


class PlanFormatError(ValueError):
    pass


@dataclass
class StatEntry:
    ids: list[str]
    highlight: str | None = None

    def to_dict(self):
        d = {"ids": list(self.ids)}
        if self.highlight is not None:
            d["highlight"] = self.highlight
        return d


@dataclass
class PlannedSlide:
    source_slide: int
    layout: str
    slots: dict = field(default_factory=dict)  # slot name -> [id] or [StatEntry]

    def ids(self):
        out = []
        for entries in self.slots.values():
            for e in entries:
                out.extend(e.ids if isinstance(e, StatEntry) else [e])
        return out

    def to_dict(self):
        return {
            "source_slide": self.source_slide,
            "layout": self.layout,
            "slots": {
                k: [e.to_dict() if isinstance(e, StatEntry) else e for e in v] for k, v in self.slots.items()
            },
        }


@dataclass
class Plan:
    slides: list[PlannedSlide]

    def to_dict(self):
        return {"slides": [s.to_dict() for s in self.slides]}


# --- parsing --------------------------------------------------------------

def parse_plan(data):
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise PlanFormatError("A plan must be an object with a 'slides' list.")
    slides = []
    for i, raw in enumerate(data["slides"], start=1):
        if not isinstance(raw, dict):
            raise PlanFormatError(f"Planned slide {i} is not an object.")
        try:
            source = int(raw.get("source_slide"))
        except (TypeError, ValueError):
            raise PlanFormatError(f"Planned slide {i} has no valid source_slide.")
        layout = str(raw.get("layout", ""))
        raw_slots = raw.get("slots") or {}
        if not isinstance(raw_slots, dict):
            raise PlanFormatError(f"Planned slide {i}: 'slots' must be an object.")
        slots = {}
        for name, entries in raw_slots.items():
            if entries is None:
                continue
            if not isinstance(entries, list):
                entries = [entries]
            slots[name] = [_parse_entry(e, i, name) for e in entries]
        slides.append(PlannedSlide(source_slide=source, layout=layout, slots=slots))
    return Plan(slides=slides)


def _parse_entry(entry, i, slot):
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and "ids" in entry:
        ids = entry["ids"] if isinstance(entry["ids"], list) else [entry["ids"]]
        highlight = entry.get("highlight")
        return StatEntry(ids=[str(x) for x in ids], highlight=str(highlight) if highlight else None)
    raise PlanFormatError(f"Planned slide {i}, slot '{slot}': entries must be ID strings or stat objects.")


# --- helpers ---------------------------------------------------------------

def _plannable(deck):
    return {x.id: x for x in deck.all_items() if not (x.kind == "text" and x.role == "footer")}


def plannable_ids(deck):
    return list(_plannable(deck))


def _footer_ids(deck):
    return {t.id for s in deck.slides for t in s.texts if t.role == "footer"}


def _layout(name):
    if name == "blank":
        return BLANK
    return LAYOUTS.get(name)


def _accepts(slot, item):
    if slot.accepts in ("text", "stat"):
        return item.kind == "text"
    return item.kind in ("table", "chart", "image")


def _highlight_ok(highlight, item):
    """A highlight must be the exact start of the paragraph, ending at a word boundary."""
    h = normalize_text(highlight)
    text = normalize_text(item.text)
    return bool(h) and text.startswith(h) and (len(text) == len(h) or text[len(h)] == " ")


# --- validation -----------------------------------------------------------

def validate_plan(plan, deck):
    items = _plannable(deck)
    footers = _footer_ids(deck)
    slide_numbers = {s.number for s in deck.slides}
    errors = []
    seen = Counter()
    prev_source = 0

    for idx, ps in enumerate(plan.slides, start=1):
        label = f"Planned slide {idx} (source slide {ps.source_slide})"
        if ps.source_slide not in slide_numbers:
            errors.append(f"{label}: there is no source slide {ps.source_slide}")
        if ps.source_slide < prev_source:
            errors.append(f"{label}: slides are out of order; source_slide must never decrease")
        prev_source = max(prev_source, ps.source_slide)

        layout = _layout(ps.layout)
        if layout is None:
            errors.append(f"{label}: unknown layout '{ps.layout}' (allowed: {', '.join(LAYOUTS)})")

        for slot_name, entries in ps.slots.items():
            slot = layout.slot(slot_name) if layout else None
            if layout and slot is None:
                errors.append(
                    f"{label}: layout '{ps.layout}' has no slot '{slot_name}' "
                    f"(allowed: {', '.join(layout.slot_names())})"
                )
            if slot and slot.max_items is not None and len(entries) > slot.max_items:
                errors.append(f"{label}: slot '{slot_name}' takes at most {slot.max_items} items")
            for entry in entries:
                is_stat = isinstance(entry, StatEntry)
                if slot and (slot.accepts == "stat") != is_stat:
                    errors.append(f"{label}: slot '{slot_name}' has the wrong entry format")
                ids = entry.ids if is_stat else [entry]
                if is_stat and not ids:
                    errors.append(f"{label}: a stat entry in '{slot_name}' has no IDs")
                for item_id in ids:
                    seen[item_id] += 1
                    item = items.get(item_id)
                    if item is None:
                        if item_id in footers:
                            errors.append(f"{label}: '{item_id}' is a footer; footers are placed automatically")
                        else:
                            errors.append(f"{label}: '{item_id}' is an unknown ID")
                        continue
                    if slot and not _accepts(slot, item):
                        errors.append(f"{label}: '{item_id}' is a {item.kind} and can't go in slot '{slot_name}'")
                    if item.slide != ps.source_slide:
                        errors.append(
                            f"{label}: '{item_id}' belongs to slide {item.slide}, "
                            f"so it can only go on slides planned from source slide {item.slide}"
                        )
                if is_stat and entry.highlight is not None and ids and ids[0] in items:
                    if not _highlight_ok(entry.highlight, items[ids[0]]):
                        errors.append(
                            f"{label}: highlight '{entry.highlight}' is not the start of the text of '{ids[0]}'"
                        )

        if layout:
            for slot in layout.slots:
                if slot.min_items and len(ps.slots.get(slot.name, [])) < slot.min_items:
                    errors.append(f"{label}: slot '{slot.name}' needs at least {slot.min_items} item(s)")

    for item_id in items:
        if seen[item_id] == 0:
            errors.append(f"'{item_id}' is missing from the plan (every content ID must appear exactly once)")
        elif seen[item_id] > 1:
            errors.append(f"'{item_id}' appears more than once (every content ID must appear exactly once)")

    planned_sources = {ps.source_slide for ps in plan.slides}
    for n in sorted(slide_numbers - planned_sources):
        errors.append(f"Source slide {n} is missing from the plan")
    return errors


# --- repair ---------------------------------------------------------------

def repair_plan(plan, deck):
    """Deterministically fix a plan. Returns (plan, list of repair notes)."""
    from .heuristic import plan_items

    plan = copy.deepcopy(plan)
    items = _plannable(deck)
    slide_numbers = [s.number for s in deck.slides]
    order = {item_id: i for i, item_id in enumerate(items)}
    repairs = []

    # 1. unknown layouts and source slides
    kept = []
    for idx, ps in enumerate(plan.slides, start=1):
        if ps.source_slide not in slide_numbers:
            repairs.append(f"Removed planned slide {idx}: no source slide {ps.source_slide}")
            continue
        if _layout(ps.layout) is None:
            own = [items[i] for i in ps.ids() if i in items and items[i].slide == ps.source_slide]
            replacement = plan_items(ps.source_slide, own) if own else []
            new_layout = replacement[0].layout if replacement else "blank"
            repairs.append(f"Planned slide {idx}: unknown layout '{ps.layout}' replaced with '{new_layout}'")
            kept.extend(replacement or [PlannedSlide(ps.source_slide, "blank", {})])
            continue
        kept.append(ps)
    plan.slides = kept

    # 2. drop invalid entries (unknown, footer, duplicate, wrong slide, wrong kind, bad slot)
    seen = set()
    for idx, ps in enumerate(plan.slides, start=1):
        layout = _layout(ps.layout)
        new_slots = {}
        for slot_name, entries in ps.slots.items():
            slot = layout.slot(slot_name)
            if slot is None:
                repairs.append(f"Planned slide {idx}: removed unknown slot '{slot_name}'")
            cleaned = []
            for entry in entries:
                is_stat = isinstance(entry, StatEntry)
                ids = entry.ids if is_stat else [entry]
                good = []
                for item_id in ids:
                    item = items.get(item_id)
                    reason = None
                    if item is None:
                        reason = "unknown or footer ID"
                    elif item_id in seen:
                        reason = "duplicate"
                    elif item.slide != ps.source_slide:
                        reason = f"belongs to slide {item.slide}"
                    elif slot is None or not _accepts(slot, item):
                        reason = f"can't go in slot '{slot_name}'"
                    if reason:
                        repairs.append(f"Planned slide {idx}: removed '{item_id}' ({reason})")
                        continue
                    seen.add(item_id)
                    good.append(item_id)
                if slot is None or not good:
                    continue
                if slot.accepts == "stat":
                    highlight = entry.highlight if is_stat else None
                    if highlight is not None and not _highlight_ok(highlight, items[good[0]]):
                        repairs.append(f"Planned slide {idx}: dropped invalid highlight '{highlight}'")
                        highlight = None
                    cleaned.append(StatEntry(ids=good, highlight=highlight))
                else:
                    cleaned.extend(good)
            if cleaned:
                new_slots[slot_name] = cleaned
        ps.slots = new_slots

    # 3. slide order
    sorted_slides = sorted(plan.slides, key=lambda p: p.source_slide)
    if [id(p) for p in sorted_slides] != [id(p) for p in plan.slides]:
        repairs.append("Reordered slides to match the original order")
    plan.slides = sorted_slides

    # 4. source slides missing entirely
    planned = {p.source_slide for p in plan.slides}
    for n in slide_numbers:
        if n in planned:
            continue
        own = [x for x in items.values() if x.slide == n and x.id not in seen]
        new = plan_items(n, own, is_first_slide=(n == slide_numbers[0])) if own else [PlannedSlide(n, "blank", {})]
        seen.update(i for p in new for i in p.ids())
        insert_at = next((i for i, p in enumerate(plan.slides) if p.source_slide > n), len(plan.slides))
        plan.slides[insert_at:insert_at] = new
        repairs.append(f"Added missing source slide {n}")

    # 5. put back missing IDs next to their original neighbours
    for item_id in items:
        if item_id not in seen:
            _reinsert(plan, items[item_id], items, order)
            seen.add(item_id)
            repairs.append(f"Re-added missing '{item_id}'")

    # 6. capacity limits and minimums
    _enforce_limits(plan, items, repairs)
    return plan, repairs


def _locate(plan, item_id):
    for p_idx, ps in enumerate(plan.slides):
        for slot_name, entries in ps.slots.items():
            for e_idx, e in enumerate(entries):
                if e == item_id or (isinstance(e, StatEntry) and item_id in e.ids):
                    return p_idx, slot_name, e_idx
    return None


def _reinsert(plan, item, items, order):
    same_slide = sorted((i for i in items.values() if i.slide == item.slide), key=lambda i: order[i.id])
    pos = same_slide.index(item)
    before = [i for i in reversed(same_slide[:pos])]
    after = same_slide[pos + 1:]

    def fits(slot_name, layout):
        slot = layout.slot(slot_name)
        if slot is None or slot.accepts == "stat" or not _accepts(slot, item):
            return False
        if item.kind == "text" and slot_name == "title" and item.role != "title":
            return False
        return True

    for neighbours, offset in ((before, 1), (after, 0)):
        for nb in neighbours:
            loc = _locate(plan, nb.id)
            if loc is None:
                continue
            p_idx, slot_name, e_idx = loc
            ps = plan.slides[p_idx]
            if fits(slot_name, _layout(ps.layout)):
                ps.slots[slot_name].insert(e_idx + offset, item.id)
                return
            break  # nearest placed neighbour doesn't fit; try the other direction

    own = [ps for ps in plan.slides if ps.source_slide == item.slide]
    for ps in own:
        layout = _layout(ps.layout)
        if item.kind == "text":
            names = (["title"] if item.role == "title" else []) + TEXT_SLOT_PREFERENCE
            for name in names:
                if layout.slot(name):
                    ps.slots.setdefault(name, []).append(item.id)
                    return
        else:
            if layout.slot("visuals"):
                ps.slots.setdefault("visuals", []).append(item.id)
                return
            if ps.layout in ("bullets", "blank"):
                ps.layout = VISUAL_LAYOUT_FOR_KIND[item.kind]
                ps.slots["visuals"] = [item.id]
                return

    new = (
        PlannedSlide(item.slide, "bullets", {"body": [item.id]})
        if item.kind == "text"
        else PlannedSlide(item.slide, VISUAL_LAYOUT_FOR_KIND[item.kind], {"visuals": [item.id]})
    )
    last = max(i for i, ps in enumerate(plan.slides) if ps.source_slide == item.slide)
    plan.slides.insert(last + 1, new)


def _enforce_limits(plan, items, repairs):
    i = 0
    while i < len(plan.slides):
        ps = plan.slides[i]
        layout = _layout(ps.layout)
        overflow = []
        for slot in layout.slots:
            entries = ps.slots.get(slot.name, [])
            if slot.max_items is not None and len(entries) > slot.max_items:
                extra = entries[slot.max_items:]
                ps.slots[slot.name] = entries[: slot.max_items]
                if slot.name == "title" and layout.first_slot("text"):
                    target = layout.first_slot("text").name
                    ps.slots[target] = extra + ps.slots.get(target, [])
                else:
                    overflow.append((slot.name, extra))
                repairs.append(f"Slot '{slot.name}' over capacity; moved {len(extra)} item(s)")

        # layouts whose required slot is empty fall back to a simpler layout
        for slot in layout.slots:
            if slot.min_items and not ps.slots.get(slot.name):
                texts = [x for x in ps.ids() if items[x].kind == "text"]
                title = ps.slots.get("title", [])
                body = [x for x in texts if x not in title]
                if ps.layout == "quote" and ps.slots.get("attribution"):
                    ps.slots = {"quote": ps.slots.pop("attribution")}
                else:
                    ps.layout = "bullets"
                    ps.slots = {k: v for k, v in (("title", title), ("body", body)) if v}
                repairs.append(f"Slot '{slot.name}' was empty; layout changed to '{ps.layout}'")
                break

        for slot_name, extra in overflow:
            if slot_name == "stats":
                new = PlannedSlide(ps.source_slide, "stats", {"stats": extra})
            elif slot_name == "visuals":
                new = PlannedSlide(ps.source_slide, ps.layout, {"visuals": extra})
            else:
                new = PlannedSlide(ps.source_slide, "bullets", {"body": extra})
            plan.slides.insert(i + 1, new)

        if not ps.ids() and ps.layout != "blank":
            if sum(1 for p in plan.slides if p.source_slide == ps.source_slide) > 1:
                plan.slides.pop(i)
                continue
            ps.layout, ps.slots = "blank", {}
        i += 1
