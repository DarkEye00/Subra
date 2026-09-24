"""Step 4: build the polished, fully editable deck from a plan.

Input is a layout plan plus the content items it refers to (model.py), not a
.pptx, so the future doc-to-deck feature can reuse this unchanged.

Coordinates are fixed per layout (inches on a 13.333 x 7.5 slide). Text sizes
are fitted with real font metrics (textfit.py); slides whose text still
overflows at the minimum size are flagged in the report.
"""

import io
import math
import re
from dataclasses import dataclass, field

from pptx.chart.data import BubbleChartData, CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_MARKER_STYLE
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

from ..plan import StatEntry
from ..verify import normalize_text
from .textfit import BULLET_INDENT_EM, LEVEL_INDENT_PT, FitPara, fit_size, line_height_ratio, measure, text_width
from .themes import SLIDE_H_IN, SLIDE_W_IN, base_presentation, mix

LAYOUT_TITLE_SLIDE, LAYOUT_TITLE_ONLY, LAYOUT_BLANK = 0, 5, 6

MX = 0.75  # side margin
CONTENT_W = SLIDE_W_IN - 2 * MX
TITLE_BOX = (MX, 0.4, CONTENT_W, 1.15)
RULE = (MX, 1.68, 0.9, 0.06)
CONTENT_TOP = 2.0
CONTENT_TOP_NO_TITLE = 0.75
CONTENT_BOTTOM = 6.8
FOOTER_BOX = (MX, 6.97, 8.5, 0.33)
GAP = 0.35
PAD = 0.3

NUMERIC_CELL = re.compile(r"^[\s(]*[-+]?(?:KES|KSh|Ksh|USD|\$|€|£)?\s?[\d][\d,.]*\s?(?:%|m|bn|k|x)?\)?\s*$")


@dataclass
class Box:
    x: float
    y: float
    w: float
    h: float

    def inset(self, d):
        return Box(self.x + d, self.y + d, self.w - 2 * d, self.h - 2 * d)

    def emu(self):
        return Inches(self.x), Inches(self.y), Inches(self.w), Inches(self.h)


@dataclass
class TStyle:
    font: str
    color: str
    max_pt: float
    min_pt: float
    bold: bool = False
    italic: bool = False
    override_runs: bool = False  # ignore run-level bold/italic (titles)
    align: object = PP_ALIGN.LEFT
    anchor: object = MSO_ANCHOR.TOP
    line_spacing: float = 1.0
    para_space_em: float = 0.4
    bullets: bool = True
    bullet_color: str | None = None


@dataclass
class RenderReport:
    slides: list = field(default_factory=list)  # [{index, source_slide, layout, overflow}]

    @property
    def overflow_slides(self):
        return [s["index"] for s in self.slides if s["overflow"]]


def render(plan, deck, theme, output_path):
    r = _Renderer(deck, theme)
    report = r.render(plan)
    r.prs.save(str(output_path))
    return report


def _rgb(hex_color):
    return RGBColor.from_string(hex_color)


class _Renderer:
    def __init__(self, deck, theme):
        self.deck = deck
        self.t = theme
        self.items = {x.id: x for x in deck.all_items()}
        self.sources = {s.number: s for s in deck.slides}
        self.prs = base_presentation(theme)
        self.number_pos = _numbering_positions(deck)
        self.image_side = 0
        self.overflow = False

    # --- driver -----------------------------------------------------------

    def render(self, plan):
        report = RenderReport()
        started = set()
        for index, ps in enumerate(plan.slides, start=1):
            self.overflow = False
            self.dark = False
            method = getattr(self, f"_layout_{ps.layout}")
            slide = method(ps)
            src = self.sources[ps.source_slide]
            if ps.source_slide not in started:
                started.add(ps.source_slide)
                if src.notes:
                    slide.notes_slide.notes_text_frame.text = src.notes
                footers = [t for t in src.texts if t.role == "footer"]
                if footers:
                    color = self.t.on_primary_muted if self.dark else self.t.muted
                    self._text(slide, Box(*FOOTER_BOX), footers,
                               TStyle(self.t.body_font, color, 10, 8, bullets=False, para_space_em=0.1,
                                      anchor=MSO_ANCHOR.MIDDLE))
            if src.hidden:
                slide._element.set("show", "0")
            report.slides.append(
                {"index": index, "source_slide": ps.source_slide, "layout": ps.layout, "overflow": self.overflow}
            )
        return report

    def _ids(self, ps, slot):
        return [self.items[i] for i in ps.slots.get(slot, [])]

    # --- slide scaffolding ------------------------------------------------

    def _new_slide(self, layout_idx, bg):
        slide = self.prs.slides.add_slide(self.prs.slide_layouts[layout_idx])
        fill = slide.background.fill
        fill.solid()
        fill.fore_color.rgb = _rgb(bg)
        return slide

    def _content_slide(self, ps):
        """Standard slide: title at top (in the real title placeholder), content area below."""
        t = self.t
        titles = self._ids(ps, "title")
        slide = self._new_slide(LAYOUT_TITLE_ONLY if titles else LAYOUT_BLANK, t.bg)
        self._decor_content(slide)
        if titles:
            self._text(
                slide, Box(*TITLE_BOX), titles,
                TStyle(t.heading_font, t.title, 32, 20, bold=t.heading_bold, override_runs=True,
                       anchor=MSO_ANCHOR.BOTTOM, line_spacing=0.95, para_space_em=0.1, bullets=False),
                shape=slide.shapes.title,
            )
            self._rect(slide, Box(*RULE), t.accent)
            top = CONTENT_TOP
        else:
            top = CONTENT_TOP_NO_TITLE
        return slide, Box(MX, top, CONTENT_W, CONTENT_BOTTOM - top)

    def _body_style(self, max_pt=28, min_pt=12, color=None, anchor=MSO_ANCHOR.TOP):
        t = self.t
        return TStyle(t.body_font, color or t.text, max_pt, min_pt, anchor=anchor, bullet_color=t.accent)

    # --- decoration -------------------------------------------------------

    def _rect(self, slide, box, color, shape=MSO_SHAPE.RECTANGLE, radius=None):
        sp = slide.shapes.add_shape(shape, *box.emu())
        sp.fill.solid()
        sp.fill.fore_color.rgb = _rgb(color)
        sp.line.fill.background()
        style = sp._element.find(qn("p:style"))
        if style is not None:
            sp._element.remove(style)
        if radius is not None:
            sp.adjustments[0] = radius
        return sp

    def _card(self, slide, box, strip=True):
        self._rect(slide, box, self.t.surface, MSO_SHAPE.ROUNDED_RECTANGLE, radius=0.04)
        if strip:
            self._rect(slide, Box(box.x + 0.25, box.y, 0.7, 0.07), self.t.accent)

    def _decor_content(self, slide):
        t = self.t
        if t.decor == "circles":
            self._rect(slide, Box(0, 0, 0.12, SLIDE_H_IN), t.primary)
        elif t.decor == "bands":
            self._rect(slide, Box(0, SLIDE_H_IN - 0.1, SLIDE_W_IN, 0.1), t.accent)

    def _decor_cover(self, slide, dark):
        t = self.t
        if t.decor == "circles":
            self._rect(slide, Box(8.3, -1.6, 8.2, 8.2), mix(t.primary, t.on_primary, 0.07), MSO_SHAPE.OVAL)
            self._rect(slide, Box(11.3, 4.9, 1.6, 1.6), t.accent, MSO_SHAPE.OVAL)
        elif t.decor == "bands":
            for i, color in enumerate((t.accent, mix(t.primary, t.accent, 0.5), mix(t.primary, "000000", 0.3))):
                self._rect(slide, Box(0, SLIDE_H_IN - 0.75 + i * 0.25, SLIDE_W_IN, 0.25), color)
        elif t.decor == "block" and not dark:
            self._rect(slide, Box(9.4, 0, SLIDE_W_IN - 9.4, SLIDE_H_IN), t.primary)

    # --- layouts ----------------------------------------------------------

    def _layout_blank(self, ps):
        return self._new_slide(LAYOUT_BLANK, self.t.bg)

    def _layout_title(self, ps):
        return self._cover(ps, section=False)

    def _layout_section(self, ps):
        return self._cover(ps, section=True)

    def _cover(self, ps, section):
        t = self.t
        dark = section or t.cover_on_primary
        self.dark = dark
        slide = self._new_slide(LAYOUT_TITLE_SLIDE, t.primary if dark else t.bg)
        self._decor_cover(slide, dark)
        text_w = 8.2 if t.decor in ("block", "circles") else CONTENT_W
        title_ph = slide.shapes.title
        sub_ph = next(p for p in slide.placeholders if p.placeholder_format.idx == 1)
        titles, subs = self._ids(ps, "title"), self._ids(ps, "subtitle")
        big = (44, 26) if section else (54, 30)
        if titles:
            self._text(
                slide, Box(MX, 1.3, text_w, 3.2), titles,
                TStyle(t.heading_font, t.on_primary if dark else t.title, *big, bold=t.heading_bold,
                       override_runs=True, anchor=MSO_ANCHOR.BOTTOM, line_spacing=0.95, para_space_em=0.15,
                       bullets=False),
                shape=title_ph,
            )
        else:
            title_ph._element.getparent().remove(title_ph._element)
        self._rect(slide, Box(MX, 4.72, 1.2, 0.08), t.accent)
        if subs:
            self._text(
                slide, Box(MX, 5.0, text_w, 1.5), subs,
                TStyle(t.body_font, t.on_primary_muted if dark else t.muted, 24, 14, bullets=False,
                       para_space_em=0.25),
                shape=sub_ph,
            )
        else:
            sub_ph._element.getparent().remove(sub_ph._element)
        return slide

    def _layout_quote(self, ps):
        t = self.t
        slide = self._new_slide(LAYOUT_BLANK, t.bg)
        self._decor_content(slide)
        self._rect(slide, Box(MX, 1.5, 0.1, 4.0), t.accent)
        self._text(
            slide, Box(MX + 0.5, 1.3, CONTENT_W - 1.0, 3.6), self._ids(ps, "quote"),
            TStyle(t.heading_font, t.title, 48, 20, anchor=MSO_ANCHOR.MIDDLE, line_spacing=1.0,
                   para_space_em=0.3, bullets=False),
        )
        attribution = self._ids(ps, "attribution")
        if attribution:
            self._text(
                slide, Box(MX + 0.5, 5.1, CONTENT_W - 1.0, 1.3), attribution,
                TStyle(t.body_font, t.muted, 20, 12, bullets=False, para_space_em=0.2),
            )
        return slide

    def _layout_bullets(self, ps):
        slide, area = self._content_slide(ps)
        body = self._ids(ps, "body")
        if not body:
            return slide
        style = self._body_style()
        single = self._fit(body, style, area)
        split = _balanced_split(body) if len(body) >= 6 else None
        if split:
            colw = (area.w - GAP * 2) / 2
            boxes = [Box(area.x, area.y, colw, area.h), Box(area.x + colw + GAP * 2, area.y, colw, area.h)]
            parts = [body[:split], body[split:]]
            fits = [self._fit(p, style, b) for p, b in zip(parts, boxes)]
            size = min(f.size for f in fits)
            if size >= single.size + 3 or (not single.fits and all(f.fits for f in fits)):
                for p, b in zip(parts, boxes):
                    self._text(slide, b, p, style, size=size)
                return slide
        self._text(slide, area, body, style)
        return slide

    def _layout_two_column(self, ps):
        t = self.t
        slide, area = self._content_slide(ps)
        colw = (area.w - GAP) / 2
        heading_style = TStyle(t.heading_font, t.highlight, 24, 16, bold=True, bullets=False, para_space_em=0.1,
                               anchor=MSO_ANCHOR.TOP)
        body_style = self._body_style(24, 12)
        columns = []
        for i, side in enumerate(("left", "right")):
            card = Box(area.x + i * (colw + GAP), area.y, colw, area.h)
            self._card(slide, card)
            inner = card.inset(PAD)
            inner = Box(inner.x, inner.y + 0.1, inner.w, inner.h - 0.1)
            heads, body = self._ids(ps, f"{side}_title"), self._ids(ps, side)
            used = 0.0
            if heads:
                hbox = Box(inner.x, inner.y, inner.w, min(1.2, inner.h * 0.3))
                fit = self._text(slide, hbox, heads, heading_style)
                used = fit.height / 72 + 0.2
            columns.append((Box(inner.x, inner.y + used, inner.w, inner.h - used), body))
        fitted = [self._fit(body, body_style, box).size for box, body in columns if body]
        size = min(fitted) if fitted else None
        for box, body in columns:
            if body:
                self._text(slide, box, body, body_style, size=size)
        return slide

    def _layout_stats(self, ps):
        t = self.t
        slide, area = self._content_slide(ps)
        entries = ps.slots.get("stats", [])
        body = self._ids(ps, "body")
        parts = [self._stat_parts(e) for e in entries]
        values = ["".join(r.text for r in runs) for runs, _ in parts]
        stats_area = Box(area.x, area.y, area.w, area.h * (0.62 if body else 1.0))

        def value_size_for(cols):
            inner_w = (stats_area.w - GAP * (cols - 1)) / cols - 2 * PAD
            size = 60.0
            while size > 24 and any(
                text_width(v, t.heading_font, size, bold=True) > inner_w * 72 * 0.94 for v in values
            ):
                size -= 2
            return size

        n = max(len(entries), 1)
        cols, rows = n, 1
        value_size = value_size_for(n)
        if n == 4 and value_size < 40:
            cols, rows = 2, 2
            value_size = value_size_for(2)
        card_w = (stats_area.w - GAP * (cols - 1)) / cols
        max_card_h = (stats_area.h - GAP * (rows - 1)) / rows
        value_h = value_size * line_height_ratio(t.heading_font) / 72 + 0.1
        label_style = TStyle(t.body_font, t.text, 20, 11, bullets=False, para_space_em=0.25)
        label_box = Box(0, 0, card_w - 2 * PAD, max_card_h - 2 * PAD - 0.15 - value_h)
        label_fits = [self._fit(labels, label_style, label_box) for _, labels in parts if labels]
        label_size = min((f.size for f in label_fits), default=16)
        label_h = max((f.height for f in label_fits), default=0) / 72
        card_h = min(max(value_h + label_h + 2 * PAD + 0.35, 1.9), max_card_h)
        block_h = card_h * rows + GAP * (rows - 1)
        top = stats_area.y + (0 if body else (stats_area.h - block_h) * 0.45)
        cards = [
            Box(stats_area.x + (i % cols) * (card_w + GAP), top + (i // cols) * (card_h + GAP), card_w, card_h)
            for i in range(n)
        ]
        for card in cards:
            self._card(slide, card)
        for (value_runs, labels), card, entry in zip(parts, cards, entries):
            inner = card.inset(PAD)
            inner = Box(inner.x, inner.y + 0.15, inner.w, inner.h - 0.15)
            self._write_stat(slide, inner, value_runs, labels, entry, value_size, label_size)
        if body:
            body_top = top + block_h + 0.35
            self._text(slide, Box(area.x, body_top, area.w, area.y + area.h - body_top), body,
                       self._body_style(20, 12))
        return slide

    def _stat_parts(self, entry):
        """Returns (value_runs, label_items). Label items may include a synthetic tail of a highlight split."""
        items = [self.items[i] for i in entry.ids]
        first = items[0]
        if entry.highlight:
            head, tail = _split_runs(first, entry.highlight)
            labels = ([_TailItem(tail, first)] if tail else []) + items[1:]
            return head, labels
        return first.runs, items[1:]

    def _write_stat(self, slide, box, value_runs, labels, entry, value_size, label_size):
        t = self.t
        shape = slide.shapes.add_textbox(*box.emu())
        tf = shape.text_frame
        _prepare_frame(tf, MSO_ANCHOR.TOP, fits=True)
        tf.clear()
        p = tf.paragraphs[0]
        _no_bullet(p, 0)
        for run in value_runs:
            self._run(p, run, t.heading_font, value_size, t.highlight, bold=True, italic=False, override=True)
        rest = labels
        if entry.highlight and labels and isinstance(labels[0], _TailItem):
            p._p.add_br()
            for run in labels[0].runs:
                self._run(p, run, t.body_font, label_size, t.text, bold=False, italic=False)
            rest = labels[1:]
        for item in rest:
            q = tf.add_paragraph()
            q.space_before = Pt(label_size * 0.4)
            _no_bullet(q, 0)
            for run in item.runs:
                self._run(q, run, t.body_font, label_size, t.text, bold=False, italic=False)

    def _layout_table(self, ps):
        slide, area = self._content_slide(ps)
        visuals, body = self._ids(ps, "visuals"), self._ids(ps, "body")
        if body:
            style = self._body_style(18, 11)
            body_h = min(self._fit(body, style, Box(0, 0, area.w, 1.4)).height / 72 + 0.1, 1.4)
            self._text(slide, Box(area.x, area.y + area.h - body_h, area.w, body_h), body, style)
            area = Box(area.x, area.y, area.w, area.h - body_h - 0.3)
        self._place_visuals(slide, visuals, area, stacked=False)
        return slide

    def _layout_chart(self, ps):
        t = self.t
        slide, area = self._content_slide(ps)
        visuals, body = self._ids(ps, "visuals"), self._ids(ps, "body")
        if body:
            vis_w = area.w * 0.63
            panel = Box(area.x + vis_w + GAP, area.y, area.w - vis_w - GAP, area.h)
            self._card(slide, panel)
            self._text(slide, panel.inset(PAD), body, self._body_style(20, 11, anchor=MSO_ANCHOR.MIDDLE))
            area = Box(area.x, area.y, vis_w, area.h)
        self._place_visuals(slide, visuals, area, stacked=bool(body))
        return slide

    def _layout_image_text(self, ps):
        slide, area = self._content_slide(ps)
        visuals, body = self._ids(ps, "visuals"), self._ids(ps, "body")
        if body:
            half = (area.w - GAP) / 2
            left_images = self.image_side % 2 == 0
            self.image_side += 1
            img_box = Box(area.x if left_images else area.x + half + GAP, area.y, half, area.h)
            txt_box = Box(area.x + half + GAP if left_images else area.x, area.y, half, area.h)
            self._text(slide, txt_box, body, self._body_style(26, 12, anchor=MSO_ANCHOR.MIDDLE))
            area = img_box
        self._place_visuals(slide, visuals, area, stacked=False)
        return slide

    # --- visuals ----------------------------------------------------------

    def _place_visuals(self, slide, visuals, area, stacked):
        n = len(visuals)
        if n == 1:
            cells = [area]
        elif n == 2 and stacked:
            h = (area.h - GAP) / 2
            cells = [Box(area.x, area.y, area.w, h), Box(area.x, area.y + h + GAP, area.w, h)]
        elif n == 2:
            w = (area.w - GAP) / 2
            cells = [Box(area.x, area.y, w, area.h), Box(area.x + w + GAP, area.y, w, area.h)]
        else:
            cols = 2
            rows = math.ceil(n / cols)
            w, h = (area.w - GAP) / cols, (area.h - GAP * (rows - 1)) / rows
            cells = [Box(area.x + (i % cols) * (w + GAP), area.y + (i // cols) * (h + GAP), w, h) for i in range(n)]
        for item, cell in zip(visuals, cells):
            if item.kind == "table":
                self._table(slide, item, cell)
            elif item.kind == "chart":
                self._chart(slide, item, cell)
            else:
                self._image(slide, item, cell)

    def _image(self, slide, item, cell):
        cl, ct, cr, cb = item.crop
        vis_w = item.px_width * (1 - cl - cr)
        vis_h = item.px_height * (1 - ct - cb)
        aspect = vis_w / vis_h if vis_w > 0 and vis_h > 0 else (item.display_aspect or 1.0)
        if cell.w / cell.h > aspect:
            h, w = cell.h, cell.h * aspect
        else:
            w, h = cell.w, cell.w / aspect
        box = Box(cell.x + (cell.w - w) / 2, cell.y + (cell.h - h) / 2, w, h)
        pic = slide.shapes.add_picture(io.BytesIO(item.blob), *box.emu())
        pic.crop_left, pic.crop_top, pic.crop_right, pic.crop_bottom = cl, ct, cr, cb
        if item.alt_text:
            pic._element.nvPicPr.cNvPr.set("descr", item.alt_text)

    def _table(self, slide, item, cell):
        t = self.t
        widths = [cell.w * f for f in item.col_widths]
        size, fits = 9.0, False
        for candidate in range(20, 8, -1):
            heights = self._row_heights(item, widths, candidate)
            if sum(heights) <= cell.h:
                size, fits = float(candidate), True
                break
        heights = self._row_heights(item, widths, size)
        if fits:  # breathe: grow rows toward a comfortable height while the table still fits
            comfy = min(0.55, cell.h / item.rows)
            heights = [max(h, comfy) for h in heights]
        if not fits:
            self.overflow = True
        gf = slide.shapes.add_table(item.rows, item.cols, Inches(cell.x), Inches(cell.y), Inches(cell.w),
                                    Inches(sum(heights)))
        table = gf.table
        table.first_row = item.header_row
        table.horz_banding = False
        for c, w in enumerate(widths):
            table.columns[c].width = Inches(w)
        for r, h in enumerate(heights):
            table.rows[r].height = Inches(h)
        for row in item.cells:
            for c in row:
                if c.row_span > 1 or c.col_span > 1:
                    table.cell(c.row, c.col).merge(table.cell(c.row + c.row_span - 1, c.col + c.col_span - 1))
        numeric_cols = self._numeric_columns(item)
        for row in item.cells:
            for c in row:
                tc = table.cell(c.row, c.col)
                header = item.header_row and c.row == 0
                if header:
                    fill, color, bold = t.primary, t.on_primary, True
                else:
                    band = (c.row - (1 if item.header_row else 0)) % 2 == 0
                    fill, color, bold = (t.surface if band else t.bg), t.text, c.bold
                tc.fill.solid()
                tc.fill.fore_color.rgb = _rgb(fill)
                tc.margin_left = tc.margin_right = Inches(0.08)
                tc.margin_top = tc.margin_bottom = Inches(0.04)
                tc.vertical_anchor = MSO_ANCHOR.MIDDLE
                align = PP_ALIGN.RIGHT if numeric_cols[c.col] and (header or NUMERIC_CELL.match(c.text)) else PP_ALIGN.LEFT
                self._plain_text(tc.text_frame, c.text, t.body_font, size, color, bold, align)

    def _numeric_columns(self, item):
        body = item.cells[1:] if item.header_row else item.cells
        out = []
        for c in range(item.cols):
            vals = [row[c].text for row in body if not row[c].spanned and row[c].text.strip()]
            out.append(bool(vals) and sum(bool(NUMERIC_CELL.match(v)) for v in vals) / len(vals) >= 0.6)
        return out

    def _row_heights(self, item, widths, size):
        margin_pt = 0.08 * 2 * 72
        ratio = line_height_ratio(self.t.body_font)
        heights = [0.0] * item.rows
        for row in item.cells:
            for c in row:
                if c.spanned:
                    continue
                w = sum(widths[c.col:c.col + c.col_span]) * 72 - margin_pt
                paras = [FitPara(runs=[(line, c.bold or c.row == 0, False)]) for line in (c.text or " ").split("\n")]
                need = measure(paras, self.t.body_font, size, w, para_space_em=0).height + 0.08 * 72
                need = max(need, size * ratio + 0.08 * 72) / c.row_span
                for r in range(c.row, c.row + c.row_span):
                    heights[r] = max(heights[r], need)
        return [h / 72 for h in heights]

    def _plain_text(self, tf, text, font, size, color, bold, align):
        tf.clear()
        tf.word_wrap = True
        for i, line in enumerate((text or "").split("\n")):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            parts = line.split("\v")
            for j, part in enumerate(parts):
                if j:
                    p._p.add_br()
                if part:
                    r = p.add_run()
                    r.text = part
                    f = r.font
                    f.size, f.name, f.bold = Pt(size), font, bool(bold)
                    f.color.rgb = _rgb(color)

    def _chart(self, slide, item, cell):
        data = None if item.rebuild == "clone" else _chart_data(item)
        if data is None:
            return self._clone_chart(slide, item, cell)
        gf = slide.shapes.add_chart(XL_CHART_TYPE[item.chart_type], *cell.emu(), data)
        self._style_chart(gf.chart, item)

    def _clone_chart(self, slide, item, cell):
        dummy = CategoryChartData()
        dummy.categories = ["a"]
        dummy.add_series("s", (1,))
        gf = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, *cell.emu(), dummy)
        part = gf.chart_part
        space = parse_xml(item.chart_xml)
        wb_rid = next(rid for rid, rel in part.rels.items() if rel.reltype == RT.PACKAGE)
        for tag in ("c:userShapes",):
            for el in space.findall(qn(tag)):
                space.remove(el)
        for el in list(space.iter(qn("a:blipFill"))):
            el.getparent().remove(el)
        ext = space.find(qn("c:externalData"))
        if item.workbook_blob:
            if ext is None:
                ext = parse_xml(
                    f'<c:externalData xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" '
                    f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" r:id="{wb_rid}">'
                    f'<c:autoUpdate val="0"/></c:externalData>'
                )
                successor = next((space.find(qn(tag)) for tag in ("c:printSettings", "c:extLst")
                                  if space.find(qn(tag)) is not None), None)
                if successor is not None:
                    successor.addprevious(ext)
                else:
                    space.append(ext)
            ext.set(qn("r:id"), wb_rid)
            part._element = space
            part.chart_workbook.update_from_xlsx_blob(item.workbook_blob)
        else:
            if ext is not None:
                space.remove(ext)
            part.drop_rel(wb_rid)
            part._element = space

    def _style_chart(self, chart, item):
        t = self.t
        palette = [_rgb(c) for c in t.chart]
        chart.font.size = Pt(14)
        chart.font.name = t.body_font
        chart.font.color.rgb = _rgb(t.text)
        if item.title:
            chart.has_title = True
            tf = chart.chart_title.text_frame
            tf.text = item.title
            for p in tf.paragraphs:
                for r in p.runs:
                    r.font.size, r.font.bold, r.font.name = Pt(16), True, t.body_font
                    r.font.color.rgb = _rgb(t.text)
        else:
            chart.has_title = False
        family = item.chart_type.split("_")[0]
        plot = chart.plots[0]
        if family in ("PIE", "DOUGHNUT"):
            chart.has_legend = True
            chart.legend.position = XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
            for i, point in enumerate(plot.series[0].points):
                point.format.fill.solid()
                point.format.fill.fore_color.rgb = palette[i % len(palette)]
            plot.has_data_labels = True
            labels = plot.data_labels
            labels.show_value, labels.show_percentage, labels.show_category_name = False, True, False
            labels.number_format, labels.number_format_is_linked = "0%", False
            labels.position = XL_LABEL_POSITION.OUTSIDE_END
            labels.font.size, labels.font.bold = Pt(14), True
            labels.font.color.rgb = _rgb(t.text)
            return
        chart.has_legend = len(item.series) > 1 or item.has_legend
        if chart.has_legend:
            chart.legend.position = XL_LEGEND_POSITION.BOTTOM
            chart.legend.include_in_layout = False
        markers_only = item.chart_type == "XY_SCATTER"
        for i, series in enumerate(plot.series):
            color = palette[i % len(palette)]
            if family in ("LINE", "RADAR") or (family == "XY" and not markers_only):
                series.format.line.color.rgb = color
                series.format.line.width = Pt(2.5)
            if (family in ("LINE", "RADAR") and "MARKERS" in item.chart_type) or (
                family == "XY" and "NO_MARKERS" not in item.chart_type
            ):
                series.marker.style = XL_MARKER_STYLE.CIRCLE
                series.marker.size = 9
                series.marker.format.fill.solid()
                series.marker.format.fill.fore_color.rgb = color
                series.marker.format.line.color.rgb = color
            if family in ("BAR", "COLUMN", "AREA", "BUBBLE"):
                series.format.fill.solid()
                series.format.fill.fore_color.rgb = color
        if family in ("BAR", "COLUMN"):
            plot.gap_width = 70
        try:
            value_axis, category_axis = chart.value_axis, chart.category_axis
        except ValueError:
            return
        value_axis.has_major_gridlines = True
        value_axis.major_gridlines.format.line.color.rgb = _rgb(t.line)
        value_axis.format.line.fill.background()
        category_axis.format.line.color.rgb = _rgb(t.line)
        for axis in (value_axis, category_axis):
            axis.tick_labels.font.size = Pt(13)
            axis.tick_labels.font.color.rgb = _rgb(t.muted)
        for axis, text in ((category_axis, item.x_axis_title), (value_axis, item.y_axis_title)):
            if text:
                axis.has_title = True
                axis.axis_title.text_frame.text = text

    # --- text -------------------------------------------------------------

    def _fit(self, items, style, box):
        base_level = min((item.level for item in items), default=0)
        paras = [
            FitPara(
                runs=[(r.text, style.bold if style.override_runs else bool(r.bold if r.bold is not None else style.bold),
                       bool(r.italic)) for r in item.runs],
                level=item.level - base_level,
                bullet=bool(style.bullets and item.bullet),
            )
            for item in items
        ]
        return fit_size(paras, style.font, box.w * 72, box.h * 72, style.max_pt, style.min_pt,
                        line_spacing=style.line_spacing, para_space_em=style.para_space_em)

    def _text(self, slide, box, items, style, shape=None, size=None):
        fit = self._fit(items, style, box)
        if size is not None:
            fit.size = size
            fit.fits = self._fit(items, TStyle(**{**style.__dict__, "max_pt": size, "min_pt": size}), box).fits
        if not fit.fits:
            self.overflow = True
        if shape is None:
            shape = slide.shapes.add_textbox(*box.emu())
        else:
            shape.left, shape.top, shape.width, shape.height = box.emu()
        tf = shape.text_frame
        _prepare_frame(tf, style.anchor, fit.fits)
        tf.clear()
        last_pos = {}
        base_level = min((item.level for item in items), default=0)
        for i, item in enumerate(items):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = style.align
            p.line_spacing = style.line_spacing
            if i:
                p.space_before = Pt(fit.size * style.para_space_em)
            bullet = item.bullet if style.bullets else None
            start_at = None
            if bullet == "number":
                pos = self.number_pos.get(item.id, 1)
                if pos != last_pos.get(item.level, 0) + 1:
                    start_at = pos
                last_pos[item.level] = pos
            _set_bullet(p, item.level - base_level, bullet, item.number_type, fit.size, style.bullet_color, start_at)
            for run in item.runs:
                self._run(p, run, style.font, fit.size, style.color, style.bold, style.italic, style.override_runs)
        return fit

    def _run(self, p, run, font, size, color, bold, italic, override=False):
        if run.br:
            p._p.add_br()
            return
        if not run.text:
            return
        r = p.add_run()
        r.text = run.text
        f = r.font
        f.size = Pt(size)
        f.name = font
        f.color.rgb = _rgb(color)
        if override:
            f.bold, f.italic = bold, italic
        else:
            f.bold = run.bold if run.bold is not None else bold
            f.italic = run.italic if run.italic is not None else italic
        if run.underline:
            f.underline = True
        if run.href:
            r.hyperlink.address = run.href


class _TailItem:
    """The label part of a highlighted stat paragraph (not a separate content item)."""

    def __init__(self, runs, source):
        self.runs = runs
        self.level = 0
        self.bullet = None
        self.text = "".join(r.text for r in runs)
        self.source = source


def _split_runs(item, highlight):
    """Split a paragraph's runs after the highlight prefix; drop the whitespace between."""
    from ..model import Run

    text = item.text
    target = normalize_text(highlight)
    cut = next((k for k in range(1, len(text) + 1) if normalize_text(text[:k]) == target), len(text))
    head, tail, pos = [], [], 0
    for run in item.runs:
        end = pos + len(run.text)
        if end <= cut:
            head.append(run)
        elif pos >= cut:
            tail.append(run)
        else:
            k = cut - pos
            head.append(Run(**{**run.__dict__, "text": run.text[:k]}))
            tail.append(Run(**{**run.__dict__, "text": run.text[k:]}))
        pos = end
    while tail and (tail[0].br or not tail[0].text.strip()):
        tail.pop(0)
    if tail:
        tail[0] = Run(**{**tail[0].__dict__, "text": tail[0].text.lstrip()})
    return head, tail


def _prepare_frame(tf, anchor, fits):
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = anchor
    tf.auto_size = MSO_AUTO_SIZE.NONE if fits else MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE


def _no_bullet(p, level):
    _set_bullet(p, level, None, None, 0, None, None)


def _set_bullet(p, level, bullet, number_type, size, color, start_at):
    pPr = p._p.get_or_add_pPr()
    for tag in ("a:buClr", "a:buSzPct", "a:buFont", "a:buNone", "a:buAutoNum", "a:buChar"):
        for el in pPr.findall(qn(tag)):
            pPr.remove(el)
    if level:
        pPr.set("lvl", str(level))
    base = level * LEVEL_INDENT_PT
    if bullet:
        hang = BULLET_INDENT_EM * size * (1.4 if bullet == "number" else 1.0)
        pPr.set("marL", str(Pt(base + hang)))
        pPr.set("indent", str(-Pt(hang)))
        if color:
            clr = pPr.makeelement(qn("a:buClr"), {})
            clr.append(clr.makeelement(qn("a:srgbClr"), {"val": color}))
            _append_bullet_el(pPr, clr)
        if bullet == "number":
            attrs = {"type": number_type or "arabicPeriod"}
            if start_at and start_at > 1:
                attrs["startAt"] = str(start_at)
            _append_bullet_el(pPr, pPr.makeelement(qn("a:buAutoNum"), attrs))
        else:
            _append_bullet_el(pPr, pPr.makeelement(qn("a:buFont"), {"typeface": "Arial"}))
            _append_bullet_el(pPr, pPr.makeelement(qn("a:buChar"), {"char": "•" if level == 0 else "–"}))
    else:
        pPr.set("marL", str(Pt(base)))
        pPr.set("indent", "0")
        _append_bullet_el(pPr, pPr.makeelement(qn("a:buNone"), {}))


def _append_bullet_el(pPr, el):
    # bullet properties go after spacing and before tabLst/defRPr/extLst
    successor = next((c for c in pPr if c.tag in (qn("a:tabLst"), qn("a:defRPr"), qn("a:extLst"))), None)
    if successor is not None:
        successor.addprevious(el)
    else:
        pPr.append(el)


def _chart_data(item):
    """python-pptx chart data for a native rebuild, or None if only a clone is safe."""
    family = item.chart_type.split("_")[0]
    if not item.series:
        return None
    if family == "XY":
        data = XyChartData()
        for s in item.series:
            if s.x_values is None or len(s.x_values) != len(s.values) or None in s.x_values or None in s.values:
                return None
            ser = data.add_series(s.name, number_format=s.number_format)
            for x, y in zip(s.x_values, s.values):
                ser.add_data_point(x, y)
        return data
    if family == "BUBBLE":
        data = BubbleChartData()
        for s in item.series:
            if not s.x_values or not s.sizes or None in s.x_values or None in s.values or None in s.sizes:
                return None
            ser = data.add_series(s.name, number_format=s.number_format)
            for x, y, z in zip(s.x_values, s.values, s.sizes):
                ser.add_data_point(x, y, z)
        return data
    if not item.categories or any(len(s.values) != len(item.categories) for s in item.series):
        return None
    data = CategoryChartData(number_format=item.series[0].number_format or "General")
    data.categories = item.categories
    for s in item.series:
        data.add_series(s.name, s.values, s.number_format)
    return data


def _numbering_positions(deck):
    """1-based position of each numbered paragraph within its original list."""
    positions = {}
    for s in deck.slides:
        counters = {}
        last_box = None
        for t in s.texts:
            if t.box != last_box:
                counters, last_box = {}, t.box
            for lvl in [k for k in counters if k > t.level]:
                del counters[lvl]
            if t.bullet == "number":
                counters[t.level] = counters.get(t.level, 0) + 1
                positions[t.id] = counters[t.level]
            else:
                counters.pop(t.level, None)
    return positions


def _balanced_split(body):
    """Index of the top-level paragraph that best halves a list, or None."""
    top = min(b.level for b in body)
    weights = [max(len(b.text), 20) for b in body]
    total = sum(weights)
    best, best_diff = None, None
    running = 0
    for i, b in enumerate(body):
        if i and b.level == top:
            diff = abs(total - 2 * running)
            if best_diff is None or diff < best_diff:
                best, best_diff = i, diff
        running += weights[i]
    return best
