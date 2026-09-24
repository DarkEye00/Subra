"""Step 2: turn a .pptx into structured content (see model.py).

Charts are read from the chart XML directly rather than through python-pptx's
plot classes, so unusual chart types still yield their data.
"""

import hashlib
from pathlib import Path

from lxml import etree
from pptx import Presentation
from pptx.chart.plot import PlotTypeInspector
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.shapes.picture import Picture

from .model import (
    ChartItem,
    ChartSeries,
    DeckContent,
    ImageItem,
    Run,
    SlideContent,
    TableCell,
    TableItem,
    TextItem,
)

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "dgm": "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
A = "{%s}" % NS["a"]
DIAGRAM_URI = "http://schemas.openxmlformats.org/drawingml/2006/diagram"

# Chart types python-pptx can build natively from data.
NATIVE_CHART_TYPES = {
    "AREA", "AREA_STACKED", "AREA_STACKED_100",
    "BAR_CLUSTERED", "BAR_STACKED", "BAR_STACKED_100",
    "COLUMN_CLUSTERED", "COLUMN_STACKED", "COLUMN_STACKED_100",
    "DOUGHNUT", "DOUGHNUT_EXPLODED",
    "LINE", "LINE_MARKERS", "LINE_MARKERS_STACKED", "LINE_MARKERS_STACKED_100",
    "LINE_STACKED", "LINE_STACKED_100",
    "PIE", "PIE_EXPLODED",
    "RADAR", "RADAR_FILLED", "RADAR_MARKERS",
    "XY_SCATTER", "XY_SCATTER_LINES", "XY_SCATTER_LINES_NO_MARKERS",
    "XY_SCATTER_SMOOTH", "XY_SCATTER_SMOOTH_NO_MARKERS",
    "BUBBLE", "BUBBLE_THREE_D_EFFECT",
}

TITLE_TYPES = {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE, PP_PLACEHOLDER.VERTICAL_TITLE}
BULLETED_TYPES = {PP_PLACEHOLDER.BODY, PP_PLACEHOLDER.OBJECT, PP_PLACEHOLDER.VERTICAL_BODY, PP_PLACEHOLDER.VERTICAL_OBJECT}
FOOTER_TYPES = {PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.DATE}
SKIP_TYPES = {PP_PLACEHOLDER.SLIDE_NUMBER, PP_PLACEHOLDER.HEADER}

DEFAULT_FONT_PT = 18.0


def _xp(el, expr):
    # python-pptx elements override xpath() with a fixed namespace map; use lxml's directly
    return etree._Element.xpath(el, expr, namespaces=NS)


def extract_deck(path):
    prs = Presentation(str(Path(path)))
    W, H = prs.slide_width, prs.slide_height
    unsupported = []
    slides = []
    for n, slide in enumerate(prs.slides, start=1):
        slides.append(_extract_slide(n, slide, W, H, unsupported))
    return DeckContent(slides=slides, slide_width=W, slide_height=H, unsupported=unsupported)


# --- slide walking ----------------------------------------------------------

class _Entry:
    """One content-bearing shape found on the slide, before IDs are assigned."""

    def __init__(self, kind, pos, **kw):
        self.kind = kind  # text | table | chart | image
        self.pos = pos  # (x, y, w, h) in slide EMU
        self.__dict__.update(kw)


def _extract_slide(n, slide, W, H, unsupported):
    entries = []
    problems = []
    _walk(slide.shapes, _identity, slide.part, entries, problems)
    _find_alternate_content(slide.shapes._spTree, problems)
    for p in dict.fromkeys(problems):
        unsupported.append(f"Slide {n}: {p}")

    _detect_title(entries, H)
    entries = _reading_order(entries, H)

    sc = SlideContent(number=n, hidden=slide._element.get("show") in ("0", "false"))
    counters = {"t": 0, "tb": 0, "c": 0, "i": 0, "b": 0}

    def next_id(kind):
        counters[kind] += 1
        return f"s{n}-{kind}{counters[kind]}"

    for e in entries:
        pos = _frac(e.pos, W, H)
        if e.kind == "text":
            box = next_id("b")
            sc.box_pos[box] = pos
            for para in e.paragraphs:
                item = TextItem(id=next_id("t"), slide=n, role=e.role, box=box, **para)
                sc.texts.append(item)
                sc.order.append(item.id)
        elif e.kind == "table":
            item = e.build(next_id("tb"), n, pos)
            sc.tables.append(item)
            sc.order.append(item.id)
        elif e.kind == "chart":
            item = e.build(next_id("c"), n, pos)
            sc.charts.append(item)
            sc.order.append(item.id)
        elif e.kind == "image":
            item = e.build(next_id("i"), n, pos)
            sc.images.append(item)
            sc.order.append(item.id)

    if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
        sc.notes = slide.notes_slide.notes_text_frame.text
    return sc


def _identity(x, y, w, h):
    return x, y, w, h


def _group_transform(grp, outer):
    xfrm = grp._element.grpSpPr.find(A + "xfrm")
    if xfrm is None or xfrm.find(A + "chExt") is None:
        return outer
    off, ext = xfrm.find(A + "off"), xfrm.find(A + "ext")
    ch_off, ch_ext = xfrm.find(A + "chOff"), xfrm.find(A + "chExt")
    ox, oy = int(off.get("x")), int(off.get("y"))
    cx, cy = int(ext.get("cx")), int(ext.get("cy"))
    chx, chy = int(ch_off.get("x")), int(ch_off.get("y"))
    chcx, chcy = int(ch_ext.get("cx")) or 1, int(ch_ext.get("cy")) or 1
    sx, sy = cx / chcx, cy / chcy

    def transform(x, y, w, h):
        return outer(ox + (x - chx) * sx, oy + (y - chy) * sy, w * sx, h * sy)

    return transform


def _shape_pos(shape, transform):
    vals = [getattr(shape, a, None) or 0 for a in ("left", "top", "width", "height")]
    return transform(*vals)


def _walk(shapes, transform, part, entries, problems):
    for shape in shapes:
        try:
            st = shape.shape_type
        except NotImplementedError:  # python-pptx can't classify some autoshapes
            st = None
        if st == MSO_SHAPE_TYPE.GROUP:
            _walk(shape.shapes, _group_transform(shape, transform), part, entries, problems)
            continue
        if st == MSO_SHAPE_TYPE.MEDIA:
            problems.append("video or audio")
            continue
        if st in (MSO_SHAPE_TYPE.EMBEDDED_OLE_OBJECT, MSO_SHAPE_TYPE.LINKED_OLE_OBJECT):
            problems.append("embedded object (for example an Excel sheet)")
            continue
        pos = _shape_pos(shape, transform)

        if isinstance(shape, Picture):
            try:
                image = shape.image
            except Exception:
                problems.append("linked image (not embedded in the file)")
                continue
            entries.append(_Entry("image", pos, build=_image_builder(shape, image)))
            continue

        if getattr(shape, "has_table", False) and shape.has_table:
            entries.append(_Entry("table", pos, build=_table_builder(shape.table)))
            continue
        if getattr(shape, "has_chart", False) and shape.has_chart:
            entries.append(_Entry("chart", pos, build=_chart_builder(shape.chart)))
            continue
        if shape._element.tag.endswith("}graphicFrame"):
            uri = shape._element.xpath(".//a:graphicData/@uri")
            if uri and uri[0] == DIAGRAM_URI:
                paras = _smartart_paragraphs_for(shape, part)
                if paras:
                    entries.append(_Entry("text", pos, role="body", paragraphs=paras, font_pt=None, placeholder=False))
            else:
                problems.append("unsupported object")
            continue

        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            role, bulleted, skip = _placeholder_role(shape)
            if skip:
                continue
            paras = _paragraphs(shape.text_frame, part, bulleted, problems)
            if paras:
                entries.append(
                    _Entry("text", pos, role=role, paragraphs=paras,
                           font_pt=_max_font_pt(shape.text_frame), placeholder=shape.is_placeholder)
                )


def _find_alternate_content(sp_tree, problems):
    # python-pptx skips shapes wrapped in mc:AlternateContent (3D models, ink, zoom, some equations)
    if _xp(sp_tree, ".//mc:AlternateContent"):
        problems.append("special object (equation, 3D model, ink or zoom)")


def _placeholder_role(shape):
    """Returns (role, bulleted_by_default, skip)."""
    if not shape.is_placeholder:
        return "body", False, False
    ph_type = shape.placeholder_format.type
    if ph_type in TITLE_TYPES:
        return "title", False, False
    if ph_type == PP_PLACEHOLDER.SUBTITLE:
        return "subtitle", False, False
    if ph_type in FOOTER_TYPES:
        return "footer", False, False
    if ph_type in SKIP_TYPES:
        return "body", False, True
    return "body", ph_type in BULLETED_TYPES, False


# --- text -----------------------------------------------------------------

def _bool_attr(el, name):
    if el is None:
        return None
    v = el.get(name)
    if v is None:
        return None
    return v in ("1", "true")


def _run_from_rpr(text, rpr, part, br=False):
    underline = None
    href = None
    if rpr is not None:
        u = rpr.get("u")
        if u is not None:
            underline = u != "none"
        link = rpr.find(A + "hlinkClick")
        rid = link.get("{%s}id" % NS["r"]) if link is not None else None
        if rid and rid in part.rels and part.rels[rid].is_external:
            href = part.rels[rid].target_ref
    return Run(
        text=text,
        bold=_bool_attr(rpr, "b"),
        italic=_bool_attr(rpr, "i"),
        underline=underline,
        href=href,
        br=br,
    )


def _runs_of(p_el, part):
    runs = []
    for child in p_el:
        tag = etree.QName(child).localname
        if tag in ("r", "fld"):
            t = child.find(A + "t")
            runs.append(_run_from_rpr(t.text or "" if t is not None else "", child.find(A + "rPr"), part))
        elif tag == "br":
            runs.append(_run_from_rpr("\v", child.find(A + "rPr"), part, br=True))
    return runs


def _bullet_of(p_el, bulleted_default):
    ppr = p_el.find(A + "pPr")
    if ppr is not None:
        if ppr.find(A + "buNone") is not None:
            return None, None
        auto = ppr.find(A + "buAutoNum")
        if auto is not None:
            return "number", auto.get("type")
        if ppr.find(A + "buChar") is not None or ppr.find(A + "buBlip") is not None:
            return "char", None
    return ("char" if bulleted_default else None), None


def _paragraphs(text_frame, part, bulleted_default, problems):
    out = []
    for p in text_frame.paragraphs:
        p_el = p._p
        if _xp(p_el, ".//mc:AlternateContent"):
            problems.append("special object (equation, 3D model, ink or zoom)")
        runs = _runs_of(p_el, part)
        text = "".join(r.text for r in runs)
        if not text.strip():
            continue
        bullet, number_type = _bullet_of(p_el, bulleted_default)
        out.append(dict(text=text, runs=runs, level=p.level, bullet=bullet, number_type=number_type))
    return out


def _max_font_pt(text_frame):
    sizes = [int(s) / 100 for s in text_frame._txBody.xpath(".//a:rPr/@sz")]
    return max(sizes) if sizes else None


# --- title detection and reading order ------------------------------------

def _detect_title(entries, H):
    texts = [e for e in entries if e.kind == "text"]
    if any(e.role == "title" for e in texts):
        return
    cands = [
        e for e in texts
        if e.role == "body" and e.pos[1] < 0.35 * H and len(e.paragraphs) <= 2
        and sum(len(p["text"]) for p in e.paragraphs) <= 150
    ]
    if not cands:
        return

    def size(e):
        return e.font_pt or DEFAULT_FONT_PT

    best = max(cands, key=lambda e: (size(e), -e.pos[1]))
    others = [e for e in texts if e is not best]
    bigger = size(best) > max((size(o) for o in others), default=0)
    topmost = all(best.pos[1] < o.pos[1] for o in others)
    if bigger or topmost:
        best.role = "title"


def _reading_order(entries, H):
    titles = [e for e in entries if getattr(e, "role", None) == "title"]
    rest = sorted((e for e in entries if e not in titles), key=lambda e: (e.pos[1], e.pos[0]))
    tolerance = 0.05 * H
    rows, row = [], []
    for e in rest:
        if row and e.pos[1] - row[0].pos[1] > tolerance:
            rows.append(row)
            row = []
        row.append(e)
    if row:
        rows.append(row)
    ordered = titles[:]
    for r in rows:
        ordered.extend(sorted(r, key=lambda e: e.pos[0]))
    return ordered


def _frac(pos, W, H):
    x, y, w, h = pos
    return (round(x / W, 3), round(y / H, 3), round(w / W, 3), round(h / H, 3))


# --- tables ---------------------------------------------------------------

def _table_builder(table):
    rows, cols = len(table.rows), len(table.columns)
    widths = [c.width for c in table.columns]
    total = sum(widths) or 1
    grid = []
    for r in range(rows):
        row = []
        for c in range(cols):
            cell = table.cell(r, c)
            runs = [run for p in cell.text_frame.paragraphs for run in p.runs if run.text.strip()]
            row.append(
                TableCell(
                    row=r,
                    col=c,
                    text=cell.text,
                    bold=bool(runs) and all(run.font.bold for run in runs),
                    row_span=cell.span_height if cell.is_merge_origin else 1,
                    col_span=cell.span_width if cell.is_merge_origin else 1,
                    spanned=cell.is_spanned,
                )
            )
        grid.append(row)

    def build(item_id, slide_no, pos):
        return TableItem(
            id=item_id, slide=slide_no, rows=rows, cols=cols, cells=grid,
            col_widths=[round(w / total, 4) for w in widths],
            header_row=bool(table.first_row), pos=pos,
        )

    return build


# --- charts ---------------------------------------------------------------

def _cache_values(el, numeric):
    """Read a c:cat / c:val / c:xVal / c:yVal / c:bubbleSize element into a list."""
    if el is None:
        return None
    caches = _xp(el, "./c:numRef/c:numCache | ./c:numLit | ./c:strRef/c:strCache | ./c:strLit")
    if not caches:
        multi = _xp(el, "./c:multiLvlStrRef/c:multiLvlStrCache/c:lvl[1]")
        if not multi:
            return None
        cache = multi[0]
        count_el = el.find(".//{%s}ptCount" % NS["c"])
    else:
        cache = caches[0]
        count_el = cache.find("{%s}ptCount" % NS["c"])
    pts = cache.findall("{%s}pt" % NS["c"])
    count = int(count_el.get("val")) if count_el is not None else len(pts)
    out = [None] * count
    for pt in pts:
        idx = int(pt.get("idx"))
        v = pt.find("{%s}v" % NS["c"])
        text = v.text if v is not None else None
        if idx >= count:
            out.extend([None] * (idx + 1 - count))
            count = idx + 1
        if text is None:
            continue
        if numeric:
            try:
                out[idx] = float(text)
            except ValueError:
                out[idx] = None
        else:
            out[idx] = text
    return out


def _format_code(el):
    if el is None:
        return None
    codes = _xp(el, "./c:numRef/c:numCache/c:formatCode/text()")
    return codes[0] if codes else None


def _rich_or_ref_text(title_el):
    if title_el is None:
        return None
    paras = _xp(title_el, "./c:tx/c:rich/a:p")
    if paras:
        text = "\n".join("".join(_xp(p, ".//a:t/text()")) for p in paras)
        return text or None
    ref = _xp(title_el, "./c:tx/c:strRef/c:strCache/c:pt/c:v/text()")
    return ref[0] if ref else None


def _series_name(ser):
    names = _xp(ser, "./c:tx/c:strRef/c:strCache/c:pt/c:v/text() | ./c:tx/c:v/text()")
    return names[0] if names else ""


def _chart_builder(chart):
    space = chart._chartSpace
    plot_area = space.find(".//{%s}plotArea" % NS["c"])
    plot_els = [el for el in plot_area if etree.QName(el).localname.endswith("Chart")]

    try:
        plot_types = [PlotTypeInspector.chart_type(p).name for p in chart.plots]
    except Exception:
        plot_types = []
    chart_type = plot_types[0] if plot_types else "UNKNOWN"

    categories = []
    multi_level = False
    series = []
    for plot_el in plot_els:
        for ser in plot_el.findall("{%s}ser" % NS["c"]):
            cat_el = ser.find("{%s}cat" % NS["c"])
            x_el = ser.find("{%s}xVal" % NS["c"])
            val_el = ser.find("{%s}val" % NS["c"])
            if val_el is None:
                val_el = ser.find("{%s}yVal" % NS["c"])
            if cat_el is not None and not categories:
                multi_level = cat_el.find("{%s}multiLvlStrRef" % NS["c"]) is not None
                categories = [c if c is not None else "" for c in (_cache_values(cat_el, numeric=False) or [])]
            x_values = None
            if x_el is not None:
                x_values = _cache_values(x_el, numeric=True)
                if x_values is not None and all(v is None for v in x_values):
                    # string x values (text labels on a scatter chart): keep as categories
                    categories = [c or "" for c in (_cache_values(x_el, numeric=False) or [])]
                    x_values = None
            series.append(
                ChartSeries(
                    name=_series_name(ser),
                    values=_cache_values(val_el, numeric=True) or [],
                    x_values=x_values,
                    sizes=_cache_values(ser.find("{%s}bubbleSize" % NS["c"]), numeric=True),
                    number_format=_format_code(val_el),
                )
            )

    title = _rich_or_ref_text(space.find("./c:chart/c:title", namespaces=NS))
    axis_titles = {}
    for ax in plot_area:
        if etree.QName(ax).localname not in ("catAx", "valAx", "dateAx", "serAx"):
            continue
        pos_el = ax.find("{%s}axPos" % NS["c"])
        side = "x" if pos_el is not None and pos_el.get("val") in ("b", "t") else "y"
        text = _rich_or_ref_text(ax.find("{%s}title" % NS["c"]))
        if text and side not in axis_titles:
            axis_titles[side] = text

    native = chart_type in NATIVE_CHART_TYPES and len(set(plot_types)) == 1 and not multi_level
    chart_part = chart.part
    workbook = chart_part.chart_workbook.xlsx_part
    chart_xml = etree.tostring(space)
    workbook_blob = workbook.blob if workbook is not None else None
    has_legend = space.find("./c:chart/c:legend", namespaces=NS) is not None

    def build(item_id, slide_no, pos):
        return ChartItem(
            id=item_id, slide=slide_no, chart_type=chart_type, categories=categories,
            series=series, title=title,
            x_axis_title=axis_titles.get("x"), y_axis_title=axis_titles.get("y"),
            has_legend=has_legend, rebuild="native" if native else "clone", pos=pos,
            chart_xml=chart_xml, workbook_blob=workbook_blob,
        )

    return build


# --- images ---------------------------------------------------------------

def _image_builder(shape, image):
    blob = image.blob
    try:
        px_w, px_h = image.size
    except Exception:
        px_w, px_h = 0, 0
    crop = (shape.crop_left, shape.crop_top, shape.crop_right, shape.crop_bottom)
    aspect = (shape.width / shape.height) if shape.width and shape.height else (px_w / px_h if px_h else 1.0)
    descr = shape._element.xpath("./p:nvPicPr/p:cNvPr/@descr")

    def build(item_id, slide_no, pos):
        return ImageItem(
            id=item_id, slide=slide_no, sha1=hashlib.sha1(blob).hexdigest(),
            content_type=image.content_type, ext=image.ext, px_width=px_w, px_height=px_h,
            crop=tuple(round(c, 4) for c in crop), display_aspect=round(aspect, 4),
            alt_text=descr[0] if descr else "", pos=pos, blob=blob,
        )

    return build


# --- SmartArt -------------------------------------------------------------

def _smartart_paragraph_elements(xml_bytes):
    """[(a:p element, level)] for SmartArt data in logical (tree) order."""
    root = etree.fromstring(xml_bytes, etree.XMLParser(resolve_entities=False, no_network=True))
    D = "{%s}" % NS["dgm"]
    pts = root.findall(f"{D}ptLst/{D}pt")
    by_id = {pt.get("modelId"): pt for pt in pts}
    content_ids = [pt.get("modelId") for pt in pts if pt.get("type") in (None, "node", "asst")]
    doc_ids = [pt.get("modelId") for pt in pts if pt.get("type") == "doc"]

    children = {}
    for cxn in root.findall(f"{D}cxnLst/{D}cxn"):
        if cxn.get("type") not in (None, "parOf"):
            continue
        children.setdefault(cxn.get("srcId"), []).append((int(cxn.get("srcOrd", "0")), cxn.get("destId")))

    ordered = []
    seen = set()

    def visit(node_id, level):
        for _, child in sorted(children.get(node_id, [])):
            if child in seen or child not in by_id:
                continue
            seen.add(child)
            if child in content_ids:
                ordered.append((child, level))
            visit(child, level + 1)

    for d in doc_ids:
        visit(d, 0)
    ordered.extend((i, 0) for i in content_ids if i not in seen)

    out = []
    for model_id, level in ordered:
        for p in by_id[model_id].findall(f"{D}t/{A}p"):
            out.append((p, level))
    return out


def parse_smartart_data(xml_bytes):
    """[(text, level)] for each non-empty SmartArt paragraph."""
    out = []
    for p, level in _smartart_paragraph_elements(xml_bytes):
        text = "".join(_xp(p, ".//a:t/text()"))
        if text.strip():
            out.append((text, level))
    return out


def _smartart_paragraphs_for(shape, part):
    rid = _xp(shape._element, ".//dgm:relIds/@r:dm")
    if not rid:
        return []
    data_part = part.related_part(rid[0])
    paras = []
    for p, level in _smartart_paragraph_elements(data_part.blob):
        runs = _runs_of(p, part)
        text = "".join(r.text for r in runs)
        if text.strip():
            paras.append(dict(text=text, runs=runs, level=level, bullet="char" if level else None, number_type=None))
    return paras
