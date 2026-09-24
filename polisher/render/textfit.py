"""Measure and fit text using real font metrics.

Themes use Office fonts (Calibri, Cambria, Arial). We measure with bundled
metric-compatible open fonts (Carlito, Caladea, Liberation Sans), which have
identical glyph widths, so a size that fits here fits in PowerPoint.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"
FONT_FILES = {
    "Calibri": "Carlito",
    "Cambria": "Caladea",
    "Arial": "LiberationSans",
}
STYLE_SUFFIX = {(False, False): "Regular", (True, False): "Bold", (False, True): "Italic", (True, True): "BoldItalic"}
REF_SIZE = 1000  # measure at a large size and scale linearly
SAFETY = 0.96  # keep a little slack for renderer differences

BULLET_INDENT_EM = 0.9  # hanging indent for the bullet glyph, in ems of the text size
LEVEL_INDENT_PT = 24.0


@lru_cache(maxsize=None)
def _font(family, bold, italic):
    base = FONT_FILES.get(family, "Carlito")
    return ImageFont.truetype(str(FONT_DIR / f"{base}-{STYLE_SUFFIX[(bool(bold), bool(italic))]}.ttf"), REF_SIZE)


@lru_cache(maxsize=100_000)
def _ref_width(text, family, bold, italic):
    return _font(family, bold, italic).getlength(text)


def text_width(text, family, size_pt, bold=False, italic=False):
    return _ref_width(text, family, bool(bold), bool(italic)) * size_pt / REF_SIZE


@lru_cache(maxsize=None)
def line_height_ratio(family):
    ascent, descent = _font(family, False, False).getmetrics()
    return (ascent + descent) / REF_SIZE


@dataclass
class FitPara:
    runs: list  # [(text, bold, italic)]
    level: int = 0
    bullet: bool = False
    scale: float = 1.0  # relative size, e.g. 0.5 for a small label under a big number


@dataclass
class Measure:
    lines: int
    height: float


@dataclass
class FitResult:
    size: float
    fits: bool
    height: float


def _tokens(runs):
    """Split runs into (word_with_trailing_space, bold, italic) tokens; "\v" becomes a break token."""
    out = []
    for text, bold, italic in runs:
        for part in re.split(r"(\v|\n)", text):
            if part in ("\v", "\n"):
                out.append(("\v", bold, italic))
            elif part:
                out.extend((w, bold, italic) for w in re.findall(r"\S+\s*|\s+", part))
    return out


def _para_lines(para, family, size, width):
    indent = para.level * LEVEL_INDENT_PT + (BULLET_INDENT_EM * size if para.bullet else 0)
    avail = max(width - indent, size)  # never less than one em
    lines, line_w = 1, 0.0
    for word, bold, italic in _tokens(para.runs):
        if word == "\v":
            lines, line_w = lines + 1, 0.0
            continue
        w_full = text_width(word, family, size, bold, italic)
        w_trim = text_width(word.rstrip(), family, size, bold, italic)
        if line_w + w_trim <= avail:
            line_w += w_full
            continue
        if line_w > 0:
            lines, line_w = lines + 1, 0.0
        if w_trim <= avail:
            line_w = w_full
            continue
        for ch in word:  # a word wider than the line: break it by characters
            cw = text_width(ch, family, size, bold, italic)
            if line_w + cw > avail and line_w > 0:
                lines, line_w = lines + 1, 0.0
            line_w += cw
    return lines


def measure(paras, family, size_pt, width_pt, line_spacing=1.0, para_space_em=0.4):
    width = width_pt * SAFETY
    ratio = line_height_ratio(family) * line_spacing
    total_lines = 0
    height = 0.0
    for i, para in enumerate(paras):
        size = size_pt * para.scale
        n = _para_lines(para, family, size, width)
        total_lines += n
        height += n * size * ratio
        if i:
            height += para_space_em * size
    return Measure(lines=total_lines, height=height)


def fit_size(paras, family, width_pt, height_pt, max_pt, min_pt, step=1.0, line_spacing=1.0, para_space_em=0.4):
    size = float(max_pt)
    while True:
        m = measure(paras, family, size, width_pt, line_spacing, para_space_em)
        if m.height <= height_pt * SAFETY:
            return FitResult(size=size, fits=True, height=m.height)
        if size - step < min_pt:
            return FitResult(size=float(min_pt), fits=False, height=m.height)
        size -= step
