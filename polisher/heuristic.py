"""A deterministic, rule-based planner.

Used offline (CLI ``--planner heuristic``), in tests, and by plan repair to
rebuild slides the AI planner left out. Claude does the real design work.
"""

import re

from .plan import Plan, PlannedSlide, StatEntry, _plannable

LEADING_NUMBER = re.compile(
    r"^\s*((?:KES|KSh|Ksh|USD|US\$|\$|€|£)?\s?[+-]?\d[\d,.]*\s?(?:%|bn|m|k|x|\+)?)(?=\s|$)"
)
AGENDA_TITLE = re.compile(r"\b(agenda|contents|outline|overview|what we.ll cover)\b", re.I)
MIN_HERO_AREA = 0.4  # share of the slide a lone picture must cover to become image_full
MAX_BODY_CHARS = 1100
MAX_BODY_PARAGRAPHS = 14
VISUAL_CAPACITY = {"table": 2, "chart": 2, "image_text": 4}


def plan_deck(deck):
    items = _plannable(deck)
    slides = []
    last = deck.slides[-1].number if deck.slides else 0
    for s in deck.slides:
        own = [items[i] for i in s.order if i in items]
        slides.extend(plan_items(s.number, own, is_first_slide=(s.number == 1), is_last_slide=(s.number == last)))
    return Plan(slides=slides)


def plan_items(source_slide, items, is_first_slide=False, is_last_slide=False):
    if not [i for i in items if i.kind != "background"]:
        return [PlannedSlide(source_slide, "blank", {})]
    texts = [i for i in items if i.kind == "text"]
    # locked objects first, so a split never pushes them off the slide's first part;
    # picture backgrounds are left out (the theme replaces them)
    visuals = sorted((i for i in items if i.kind not in ("text", "background")), key=lambda i: i.kind != "locked")
    titles = [t.id for t in texts if t.role == "title"][:3]
    rest = [t for t in texts if t.id not in titles]
    subs = [t.id for t in rest if t.role == "subtitle"]
    body = [t for t in rest if t.role != "subtitle"]

    def slots(**kw):
        return {k: v for k, v in kw.items() if v}

    if visuals:
        kinds = {v.kind for v in visuals}
        hero = visuals[0]
        if len(visuals) == 1 and hero.kind == "image" and not rest and hero.pos[2] * hero.pos[3] >= MIN_HERO_AREA:
            return [PlannedSlide(source_slide, "image_full", slots(title=titles, visuals=[hero.id]))]
        layout = "table" if kinds == {"table"} else "image_text" if kinds <= {"image", "locked"} else "chart"
        cap = VISUAL_CAPACITY[layout]
        chunks = [visuals[i:i + cap] for i in range(0, len(visuals), cap)]
        body_ids = subs + [t.id for t in body]
        out = [PlannedSlide(source_slide, layout, slots(title=titles, visuals=[v.id for v in chunks[0]], body=body_ids))]
        out += [PlannedSlide(source_slide, layout, {"visuals": [v.id for v in c]}) for c in chunks[1:]]
        return out

    if is_first_slide:
        titles = titles or [texts[0].id]
        return [PlannedSlide(source_slide, "title", slots(title=titles, subtitle=[t.id for t in texts if t.id not in titles]))]

    total_chars = sum(len(t.text) for t in texts)
    if is_last_slide and total_chars <= 120 and len(texts) <= 3:
        closing = titles or [texts[0].id]
        return [PlannedSlide(source_slide, "title", slots(title=closing, subtitle=[t.id for t in texts if t.id not in closing]))]

    if not body:
        if not titles:
            titles, subs = subs[:1], subs[1:]
        return [PlannedSlide(source_slide, "section", slots(title=titles, subtitle=subs))]

    title_text = " ".join(t.text for t in texts if t.id in titles)
    if titles and not subs and AGENDA_TITLE.search(title_text) and 3 <= len(body) <= 7 and all(t.level == 0 for t in body):
        return [PlannedSlide(source_slide, "agenda", slots(title=titles, items=[t.id for t in body]))]

    stats = _stat_entries(body)
    if stats and not subs:
        return [PlannedSlide(source_slide, "stats", slots(title=titles, stats=stats))]

    columns = _two_boxes(body)
    if columns and not subs:
        by_id = {t.id: t for t in body}
        cols = {}
        for side, ids in zip(("left", "right"), columns):
            paras = [by_id[i] for i in ids]
            if len(paras) > 1 and paras[0].level == 0 and all(p.level > 0 for p in paras[1:]):
                cols[f"{side}_title"], cols[side] = ids[:1], ids[1:]
            else:
                cols[side] = ids
        return [PlannedSlide(source_slide, "two_column", slots(title=titles, **cols))]

    chunks = _chunk_body([t for t in rest if t.id in subs] + body)
    out = [PlannedSlide(source_slide, "bullets", slots(title=titles, body=chunks[0]))]
    out += [PlannedSlide(source_slide, "bullets", {"body": c}) for c in chunks[1:]]
    return out


def _stat_entries(body):
    if not 2 <= len(body) <= 4:
        return None
    entries = []
    for t in body:
        m = LEADING_NUMBER.match(t.text)
        if not m or len(t.text) > 70 or t.level:
            return None
        value = m.group(1).strip()
        highlight = None if value == t.text.strip() else value
        entries.append(StatEntry(ids=[t.id], highlight=highlight))
    return entries


def _two_boxes(body):
    boxes = []
    for t in body:
        if not boxes or boxes[-1][0] != t.box:
            boxes.append((t.box, []))
        boxes[-1][1].append(t.id)
    if len(boxes) != 2:
        return None
    return boxes[0][1], boxes[1][1]


def _chunk_body(paragraphs):
    chunks, current, chars = [], [], 0
    for t in paragraphs:
        starts_group = t.level == 0
        if current and starts_group and (chars + len(t.text) > MAX_BODY_CHARS or len(current) >= MAX_BODY_PARAGRAPHS):
            chunks.append(current)
            current, chars = [], 0
        current.append(t.id)
        chars += len(t.text)
    if current:
        chunks.append(current)
    return chunks
