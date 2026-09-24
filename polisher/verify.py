"""Step 5: prove the polished deck still holds every piece of original content.

Pure code, no AI. Compares two extracted decks (see extract.py) as multisets,
so content may move between the slides of a split, but nothing may be lost,
changed, or added.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

CURRENCY = r"(?:KES|KSh|Ksh|KSH|USD|US\$|\$|€|£)"
# Possessive quantifiers stop "10km" from matching as "1" + "0".
NUMBER_RE = re.compile(
    rf"(?:(?P<cur>{CURRENCY})\s?)?(?P<num>\d++(?:[.,]\d++)*+)(?:(?P<suf>%|bn|m|k|b|M|K|B)(?![A-Za-z]))?"
)


def normalize_text(text):
    text = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", text).strip()


def extract_numbers(text):
    out = []
    for m in NUMBER_RE.finditer(normalize_text(text)):
        cur = m.group("cur") or ""
        num = m.group("num").replace(",", "")
        suf = m.group("suf") or ""
        if cur and cur[0].isalpha():
            cur = cur.upper()
        out.append(f"{cur}{num}{suf}")
    return out


@dataclass
class VerificationReport:
    passed: bool
    problems: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)


def verify(original, rendered, slide_sources=None, dropped_backgrounds=()):
    """Compare two extracted decks.

    ``slide_sources[i]`` is the source slide number of rendered slide i+1 (from the render
    report); with it, locked objects must also keep their shape ID and stay on their slide,
    and equation counts are checked per slide. ``dropped_backgrounds`` lists picture
    backgrounds the theme replaced on purpose (logged by the renderer).
    """
    problems = []
    problems += _check_texts(original, rendered)
    problems += _check_numbers(original, rendered)
    problems += _check_tables(original, rendered)
    problems += _check_charts(original, rendered)
    problems += _check_images(original, rendered, set(dropped_backgrounds))
    problems += _check_master_images(original, rendered)
    problems += _check_locked(original, rendered, slide_sources)
    problems += _check_notes(original, rendered)
    counts = {
        "texts": sum(len(s.texts) for s in original.slides),
        "tables": sum(len(s.tables) for s in original.slides),
        "charts": sum(len(s.charts) for s in original.slides),
        "images": sum(len(s.images) for s in original.slides),
        "locked": sum(len(s.locked) for s in original.slides),
    }
    return VerificationReport(passed=not problems, problems=problems, counts=counts)


def _short(text, limit=80):
    text = normalize_text(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _texts(deck):
    return [t for s in deck.slides for t in s.texts]


def _check_texts(original, rendered):
    orig = Counter(normalize_text(t.text) for t in _texts(original))
    new = Counter(normalize_text(t.text) for t in _texts(rendered))
    problems = []
    missing = orig - new
    for text, count in missing.items():
        ids = [t.id for t in _texts(original) if normalize_text(t.text) == text][:count]
        problems.append(f"Missing text [{', '.join(ids)}]: '{_short(text)}'")
    for text in (new - orig):
        problems.append(f"Unexpected text (not in original): '{_short(text)}'")
    return problems


def _visible_cells(table):
    return [c for row in table.cells for c in row if not c.spanned]


def _all_numbers(deck):
    nums = Counter()
    for t in _texts(deck):
        nums.update(extract_numbers(t.text))
    for s in deck.slides:
        for tbl in s.tables:
            for c in _visible_cells(tbl):
                nums.update(extract_numbers(c.text))
    return nums


def _check_numbers(original, rendered):
    orig, new = _all_numbers(original), _all_numbers(rendered)
    if orig == new:
        return []
    missing = sorted((orig - new).elements())
    extra = sorted((new - orig).elements())
    return [f"Numbers changed: missing {missing}, unexpected {extra}"]


def _table_signature(tbl):
    return (
        tbl.rows,
        tbl.cols,
        tuple(
            (normalize_text("" if c.spanned else c.text), c.row_span, c.col_span, c.spanned)
            for row in tbl.cells
            for c in row
        ),
    )


def _check_tables(original, rendered):
    new = Counter(_table_signature(t) for s in rendered.slides for t in s.tables)
    problems = []
    for s in original.slides:
        for t in s.tables:
            sig = _table_signature(t)
            if new[sig] > 0:
                new[sig] -= 1
            else:
                problems.append(f"Table [{t.id}] is missing or its cells changed")
    extra = sum(new.values())
    if extra:
        problems.append(f"{extra} unexpected table(s) not in the original")
    return problems


def _chart_family(chart_type):
    parts = chart_type.split("_")
    stacking = "STACKED_100" if "100" in parts else ("STACKED" if "STACKED" in parts else "")
    return parts[0], stacking


def _num(v):
    return None if v is None else float(f"{v:.9g}")


def _nums(values):
    return None if values is None else tuple(_num(v) for v in values)


def _chart_signature(ch):
    return (
        _chart_family(ch.chart_type),
        normalize_text(ch.title or ""),
        normalize_text(ch.x_axis_title or ""),
        normalize_text(ch.y_axis_title or ""),
        tuple(normalize_text(c) for c in ch.categories),
        tuple(
            (normalize_text(s.name), _nums(s.values), _nums(s.x_values), _nums(s.sizes))
            for s in ch.series
        ),
    )


def _check_charts(original, rendered):
    new = Counter(_chart_signature(c) for s in rendered.slides for c in s.charts)
    problems = []
    for s in original.slides:
        for c in s.charts:
            sig = _chart_signature(c)
            if new[sig] > 0:
                new[sig] -= 1
            else:
                problems.append(f"Chart [{c.id}] is missing or its data changed")
    extra = sum(new.values())
    if extra:
        problems.append(f"{extra} unexpected chart(s) not in the original")
    return problems


def _image_signature(img):
    return (img.sha256, tuple(sorted(img.crop_raw.items())), img.alt_text, img.title, img.svg_sha256)


def _check_images(original, rendered, dropped):
    new = Counter(_image_signature(i) for s in rendered.slides for i in s.images)
    new_bgs = Counter(b.sha256 for s in rendered.slides for b in s.backgrounds)
    problems = []
    for s in original.slides:
        for i in s.images:
            sig = _image_signature(i)
            if new[sig] > 0:
                new[sig] -= 1
            else:
                problems.append(f"Image [{i.id}] is missing, or its crop, alt text or SVG changed")
    for s in original.slides:
        for b in s.backgrounds:
            if b.id in dropped:
                continue
            if new_bgs[b.sha256] > 0:
                new_bgs[b.sha256] -= 1
                continue
            as_picture = next((sig for sig, n in new.items() if n > 0 and sig[0] == b.sha256), None)
            if as_picture is not None:  # kept on the slide as a regular picture
                new[as_picture] -= 1
            else:
                problems.append(f"Background image [{b.id}] is missing")
    extra = sum(new.values())
    if extra:
        problems.append(f"{extra} unexpected image(s) not in the original")
    return problems


def _check_master_images(original, rendered):
    new = Counter((m.container, m.sha256) for m in rendered.master_images)
    problems = []
    for m in original.master_images:
        key = (m.container, m.sha256)
        if new[key] > 0:
            new[key] -= 1
        else:
            problems.append(f"Image '{m.name}' on {m.container} is missing or changed")
    return problems


def _locked_signature(x):
    return (x.lock_type, tuple(x.part_hashes), tuple(x.links), x.xml_sig, x.equations)


def _check_locked(original, rendered, slide_sources):
    problems = []
    if slide_sources is None:
        new = Counter(_locked_signature(x) for s in rendered.slides for x in s.locked)
        for s in original.slides:
            for x in s.locked:
                sig = _locked_signature(x)
                if new[sig] > 0:
                    new[sig] -= 1
                else:
                    problems.append(f"Locked {x.lock_type} [{x.id}] is missing or changed")
        if sum(new.values()):
            problems.append(f"{sum(new.values())} unexpected locked object(s) not in the original")
        return problems

    # With the slide map: same object, same shape ID, on a slide made from the same source slide.
    available = Counter(
        (source, x.shape_id, _locked_signature(x))
        for s, source in zip(rendered.slides, slide_sources)
        for x in s.locked
    )
    for s in original.slides:
        for x in s.locked:
            key = (s.number, x.shape_id, _locked_signature(x))
            if available[key] > 0:
                available[key] -= 1
            else:
                problems.append(f"Locked {x.lock_type} [{x.id}] is missing, changed, or moved off slide {s.number}")
    if sum(available.values()):
        problems.append(f"{sum(available.values())} unexpected locked object(s) not in the original")

    equations = Counter()
    for s, source in zip(rendered.slides, slide_sources):
        equations[source] += sum(x.equations for x in s.locked)
    for s in original.slides:
        expected = sum(x.equations for x in s.locked)
        if equations[s.number] != expected:
            problems.append(
                f"Slide {s.number} should have {expected} equation(s) but has {equations[s.number]}"
            )
    return problems


def _check_notes(original, rendered):
    orig = [(s.number, normalize_text(s.notes)) for s in original.slides if normalize_text(s.notes)]
    new = [normalize_text(s.notes) for s in rendered.slides if normalize_text(s.notes)]
    problems = []
    for idx, (number, text) in enumerate(orig):
        if idx >= len(new) or new[idx] != text:
            problems.append(f"Speaker notes for original slide {number} are missing or changed")
    if len(new) > len(orig):
        problems.append("Unexpected speaker notes not in the original")
    return problems
