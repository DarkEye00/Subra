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


def verify(original, rendered):
    problems = []
    problems += _check_texts(original, rendered)
    problems += _check_numbers(original, rendered)
    problems += _check_tables(original, rendered)
    problems += _check_charts(original, rendered)
    problems += _check_images(original, rendered)
    problems += _check_notes(original, rendered)
    counts = {
        "texts": sum(len(s.texts) for s in original.slides),
        "tables": sum(len(s.tables) for s in original.slides),
        "charts": sum(len(s.charts) for s in original.slides),
        "images": sum(len(s.images) for s in original.slides),
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


def _check_images(original, rendered):
    new = Counter(i.sha1 for s in rendered.slides for i in s.images)
    problems = []
    for s in original.slides:
        for i in s.images:
            if new[i.sha1] > 0:
                new[i.sha1] -= 1
            else:
                problems.append(f"Image [{i.id}] is missing")
    extra = sum(new.values())
    if extra:
        problems.append(f"{extra} unexpected image(s) not in the original")
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
