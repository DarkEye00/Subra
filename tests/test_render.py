import copy

import pytest
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Inches

from deckbuilder import build_rich_deck
from polisher.extract import extract_deck
from polisher.heuristic import plan_deck
from polisher.plan import parse_plan, validate_plan
from polisher.render.renderer import render
from polisher.render.themes import THEMES, get_theme
from polisher.verify import verify


@pytest.fixture(scope="module")
def rich_path(tmp_path_factory):
    return build_rich_deck(tmp_path_factory.mktemp("r") / "rich.pptx")


@pytest.fixture(scope="module")
def rich(rich_path):
    deck = extract_deck(rich_path)
    deck.source = rich_path
    return deck


def render_to(tmp_path, deck, plan, theme="navy", name="out.pptx", source=None):
    out = tmp_path / name
    report = render(plan, deck, get_theme(theme), out, source or deck.source)
    return out, report


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_round_trip_keeps_all_content(tmp_path, rich, theme):
    out, _ = render_to(tmp_path, rich, plan_deck(rich), theme)
    result = verify(rich, extract_deck(out))
    assert result.passed, result.problems


def test_output_keeps_the_original_slide_size(tmp_path, rich, rich_path):
    out, _ = render_to(tmp_path, rich, plan_deck(rich))
    before, after = Presentation(rich_path), Presentation(out)
    assert (after.slide_width, after.slide_height) == (before.slide_width, before.slide_height)


def test_theme_fonts_are_written_into_the_office_theme(tmp_path, rich):
    out, _ = render_to(tmp_path, rich, plan_deck(rich), "navy")
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    theme_xml = Presentation(out).slide_master.part.part_related_by(RT.THEME).blob.decode()
    assert '<a:majorFont><a:latin typeface="Cambria"' in theme_xml
    assert '<a:minorFont><a:latin typeface="Calibri"' in theme_xml


def test_content_titles_use_the_title_placeholder(tmp_path, rich):
    out, _ = render_to(tmp_path, rich, plan_deck(rich))
    prs = Presentation(out)
    assert prs.slides[1].shapes.title.text == "Highlights"


def test_tables_and_charts_are_native_and_editable(tmp_path, rich):
    out, _ = render_to(tmp_path, rich, plan_deck(rich))
    prs = Presentation(out)
    assert any(getattr(s, "has_table", False) and s.has_table for s in prs.slides[2].shapes)
    assert any(getattr(s, "has_chart", False) and s.has_chart for s in prs.slides[3].shapes)


def test_hidden_slides_stay_hidden(tmp_path, rich):
    out, _ = render_to(tmp_path, rich, plan_deck(rich))
    assert extract_deck(out).slides[-1].hidden is True


def test_split_slide_round_trips(tmp_path, rich):
    plan = plan_deck(rich)
    data = plan.to_dict()
    s2 = data["slides"][1]
    s2["slots"]["body"] = ["s2-t2", "s2-t3"]
    data["slides"].insert(2, {"source_slide": 2, "layout": "bullets", "slots": {"body": ["s2-t4"]}})
    out, _ = render_to(tmp_path, rich, parse_plan(data))
    rendered = extract_deck(out)
    assert len(rendered.slides) == len(rich.slides) + 1
    assert verify(rich, rendered).passed


def test_stat_highlight_is_big_but_text_is_unchanged(tmp_path, rich):
    data = plan_deck(rich).to_dict()
    data["slides"][1] = {
        "source_slide": 2,
        "layout": "stats",
        "slots": {
            "title": ["s2-t1"],
            "stats": [{"ids": ["s2-t2"], "highlight": "Revenue grew 45%"}, {"ids": ["s2-t4"]}],
            "body": ["s2-t3"],
        },
    }
    out, _ = render_to(tmp_path, rich, parse_plan(data))
    assert verify(rich, extract_deck(out)).passed
    prs = Presentation(out)
    card = next(sh for sh in prs.slides[1].shapes if sh.has_text_frame and sh.text_frame.text.startswith("Revenue"))
    runs = [r for r in card.text_frame.paragraphs[0].runs]
    assert runs[0].text == "Revenue grew 45%"
    assert runs[0].font.size > runs[-1].font.size


def test_every_layout_round_trips(tmp_path, rich):
    data = {
        "slides": [
            {"source_slide": 1, "layout": "section", "slots": {"title": ["s1-t1"], "subtitle": ["s1-t2"]}},
            {"source_slide": 2, "layout": "two_column",
             "slots": {"title": ["s2-t1"], "left_title": ["s2-t2"], "left": ["s2-t3"], "right": ["s2-t4"]}},
            {"source_slide": 3, "layout": "table", "slots": {"title": ["s3-t1"], "visuals": ["s3-tb1"]}},
            {"source_slide": 4, "layout": "image_text", "slots": {"title": ["s4-t1"], "visuals": ["s4-c1"]}},
            {"source_slide": 5, "layout": "image_text", "slots": {"visuals": ["s5-i1"], "body": ["s5-t1"]}},
            {"source_slide": 6, "layout": "table", "slots": {"title": ["s6-t1"], "visuals": ["s6-c1"], "body": ["s6-t2"]}},
            {"source_slide": 7, "layout": "title", "slots": {"title": ["s7-t1"], "subtitle": ["s7-t2"]}},
        ]
    }
    out, _ = render_to(tmp_path, rich, parse_plan(data))
    result = verify(rich, extract_deck(out))
    assert result.passed, result.problems


def test_kept_chart_round_trips(tmp_path, rich):
    deck = copy.deepcopy(rich)
    deck.slides[3].charts[0].rebuild = "keep"
    out, _ = render_to(tmp_path, deck, plan_deck(deck))
    result = verify(deck, extract_deck(out))
    assert result.passed, result.problems


def test_overflowing_slide_is_flagged(tmp_path):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Too much"
    tf = s.placeholders[1].text_frame
    tf.text = "word " * 400
    for _ in range(30):
        tf.add_paragraph().text = "another long paragraph with many words in it " * 3
    prs.save(tmp_path / "big.pptx")
    deck = extract_deck(tmp_path / "big.pptx")
    deck.source = tmp_path / "big.pptx"
    ids = [t.id for t in deck.slides[0].texts]
    plan = parse_plan({"slides": [{"source_slide": 1, "layout": "bullets",
                                   "slots": {"title": ids[:1], "body": ids[1:]}}]})
    out, report = render_to(tmp_path, deck, plan)
    assert report.overflow_slides == [1]
    assert verify(deck, extract_deck(out)).passed


def test_footer_text_is_kept(tmp_path):
    from pptx.enum.shapes import PP_PLACEHOLDER

    prs = Presentation()
    layout = prs.slide_layouts[1]
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = "Hello"
    s.placeholders[1].text = "Body"
    ph = next(p for p in layout.placeholders if p.placeholder_format.type == PP_PLACEHOLDER.FOOTER)
    s.shapes.clone_placeholder(ph)
    next(p for p in s.placeholders if p.placeholder_format.type == PP_PLACEHOLDER.FOOTER).text = "Confidential"
    prs.save(tmp_path / "f.pptx")
    deck = extract_deck(tmp_path / "f.pptx")
    deck.source = tmp_path / "f.pptx"
    out, _ = render_to(tmp_path, deck, plan_deck(deck))
    assert verify(deck, extract_deck(out)).passed


def test_long_list_of_short_lines_flows_into_two_columns(tmp_path):
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Cover"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Operations"
    tf = s.placeholders[1].text_frame
    tf.text = "Group 0"
    for g in range(5):
        if g:
            tf.add_paragraph().text = f"Group {g}"
        for i in range(2):
            p = tf.add_paragraph()
            p.text = f"Detail {g}.{i} about the work"
            p.level = 1
    prs.save(tmp_path / "d.pptx")
    deck = extract_deck(tmp_path / "d.pptx")
    deck.source = tmp_path / "d.pptx"
    out, _ = render_to(tmp_path, deck, plan_deck(deck))
    body_boxes = [sh for sh in Presentation(out).slides[1].shapes
                  if sh.has_text_frame and sh.text_frame.text.startswith("Group")]
    assert len(body_boxes) == 2
    assert verify(deck, extract_deck(out)).passed


# --- Task 2: 12pt floor and named shapes ------------------------------------

def _run_sizes(prs, in_tables):
    """Explicit run sizes (pt) on every slide, either inside tables or outside them."""
    sizes = []
    for slide in prs.slides:
        for sh in slide.shapes:
            if getattr(sh, "has_table", False) and sh.has_table:
                if in_tables:
                    for cell in sh.table.iter_cells():
                        sizes += [r.font.size.pt for p in cell.text_frame.paragraphs for r in p.runs if r.font.size]
            elif sh.has_text_frame and not in_tables:
                sizes += [r.font.size.pt for p in sh.text_frame.paragraphs for r in p.runs if r.font.size]
    return sizes


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_no_text_below_twelve_points_on_the_business_deck(tmp_path, theme):
    from deckbuilder import build_business_deck

    src = build_business_deck(tmp_path / "b.pptx")
    deck = extract_deck(src)
    out, _ = render_to(tmp_path, deck, plan_deck(deck), theme, source=src)
    sizes = _run_sizes(Presentation(out), in_tables=False)
    assert sizes and min(sizes) >= 12
    assert min(_run_sizes(Presentation(out), in_tables=True)) >= 12


def test_long_table_floors_at_ten_points_and_is_flagged(tmp_path):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Every branch"
    tbl = s.shapes.add_table(30, 4, Inches(0.5), Inches(1.5), Inches(9), Inches(5)).table
    for r in range(30):
        for c in range(4):
            tbl.cell(r, c).text = f"Branch {r}" if c == 0 else f"{r * 10 + c},{c}00"
    prs.save(tmp_path / "t.pptx")
    deck = extract_deck(tmp_path / "t.pptx")
    out, report = render_to(tmp_path, deck, plan_deck(deck), source=tmp_path / "t.pptx")
    sizes = _run_sizes(Presentation(out), in_tables=True)
    assert 10 <= min(sizes) < 12
    assert report.overflow_slides == [1]
    assert report.slides[0]["min_pt"] < 12
    assert verify(deck, extract_deck(out)).passed


def test_drawn_shapes_are_named(tmp_path, rich):
    out, _ = render_to(tmp_path, rich, plan_deck(rich))
    names = {sh.name for slide in Presentation(out).slides for sh in slide.shapes}
    assert {"SUBRA decor", "SUBRA text", "SUBRA table", "SUBRA chart"} <= names, names
    # the chart slide with a text panel draws a card
    assert "SUBRA card" in names


# --- Task 3: agenda, timeline and image_full ---------------------------------

def _new_layouts_deck(path):
    """Cover, an agenda, a four-step journey, and one large photo with a title."""
    import io

    from deckbuilder import photo_bytes

    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Safari Fresh Foods"
    s.placeholders[1].text = "Board update"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Agenda"
    tf = s.placeholders[1].text_frame
    tf.text = "Where we are"
    for item in ("Market", "Financials", "Operations", "Plans for 2027"):
        tf.add_paragraph().text = item
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Our journey"
    tf = s.placeholders[1].text_frame
    tf.text = "2023: founded in Nairobi"
    for step in ("2024: first cold-chain hub", "2025: 12 counties served", "2026: Mombasa hub opened"):
        tf.add_paragraph().text = step
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "The Mombasa hub"
    s.shapes.add_picture(io.BytesIO(photo_bytes(seed=1)), Inches(0.5), Inches(1.5), Inches(9))
    prs.save(path)
    return path


NEW_LAYOUTS_PLAN = {"slides": [
    {"source_slide": 1, "layout": "title", "slots": {"title": ["s1-t1"], "subtitle": ["s1-t2"]}},
    {"source_slide": 2, "layout": "agenda", "slots": {"title": ["s2-t1"], "items": ["s2-t2", "s2-t3", "s2-t4", "s2-t5", "s2-t6"]}},
    {"source_slide": 3, "layout": "timeline", "slots": {"title": ["s3-t1"], "steps": ["s3-t2", "s3-t3", "s3-t4", "s3-t5"]}},
    {"source_slide": 4, "layout": "image_full", "slots": {"title": ["s4-t1"], "visuals": ["s4-i1"]}},
]}


@pytest.fixture
def new_layouts(tmp_path):
    src = _new_layouts_deck(tmp_path / "new.pptx")
    deck = extract_deck(src)
    plan = parse_plan(NEW_LAYOUTS_PLAN)
    assert validate_plan(plan, deck) == []
    return src, deck, plan


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_new_layouts_round_trip(tmp_path, new_layouts, theme):
    src, deck, plan = new_layouts
    out, report = render_to(tmp_path, deck, plan, theme, source=src)
    result = verify(deck, extract_deck(out))
    assert result.passed, result.problems
    assert report.overflow_slides == []


def test_agenda_numbers_are_bullets_not_text(tmp_path, new_layouts):
    from pptx.oxml.ns import qn

    src, deck, plan = new_layouts
    out, _ = render_to(tmp_path, deck, plan, source=src)
    slide = Presentation(out).slides[1]
    items = next(sh for sh in slide.shapes if sh.has_text_frame and sh.text_frame.text.startswith("Where we are"))
    paragraphs = items.text_frame.paragraphs
    assert len(paragraphs) == 5
    assert all(p._p.pPr is not None and p._p.pPr.find(qn("a:buAutoNum")) is not None for p in paragraphs)
    assert verify(deck, extract_deck(out)).passed  # numbers are bullets, so no text or number was added


def test_image_full_picture_fills_most_of_the_slide(tmp_path, new_layouts):
    src, deck, plan = new_layouts
    out, _ = render_to(tmp_path, deck, plan, source=src)
    prs = Presentation(out)
    pic = next(sh for sh in prs.slides[3].shapes if sh.shape_type == 13)
    assert pic.width * pic.height >= 0.5 * prs.slide_width * prs.slide_height
    assert prs.slides[3].shapes.title.text == "The Mombasa hub"


def test_timeline_draws_one_marker_per_step(tmp_path, new_layouts):
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    src, deck, plan = new_layouts
    out, _ = render_to(tmp_path, deck, plan, source=src)
    shapes = Presentation(out).slides[2].shapes
    ovals = [sh for sh in shapes if sh.name == "SUBRA decor" and sh.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE
             and sh.auto_shape_type == MSO_SHAPE.OVAL]
    assert len(ovals) == 4
    labels = [sh for sh in shapes if sh.name == "SUBRA text"]
    assert len(labels) == 4


# --- Task 4: content keeps clear of footers ---------------------------------

def test_footer_in_content_area_is_avoided(tmp_path):
    from pptx.enum.shapes import PP_PLACEHOLDER

    from polisher.qa import check_deck

    prs = Presentation()
    layout = prs.slide_layouts[1]
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Cover"
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = "Operations"
    tf = s.placeholders[1].text_frame
    tf.text = "Point one about the work we did this quarter"
    for i in range(9):
        tf.add_paragraph().text = f"Point {i + 2} about the work we did this quarter and what it cost"
    ph = next(p for p in layout.placeholders if p.placeholder_format.type == PP_PLACEHOLDER.FOOTER)
    s.shapes.clone_placeholder(ph)
    footer = next(p for p in s.placeholders if p.placeholder_format.type == PP_PLACEHOLDER.FOOTER)
    footer.text = "Confidential"
    footer.left, footer.top, footer.width, footer.height = Inches(0.5), Inches(5.5), Inches(9), Inches(0.4)
    prs.save(tmp_path / "f.pptx")
    deck = extract_deck(tmp_path / "f.pptx")
    out, report = render_to(tmp_path, deck, plan_deck(deck), source=tmp_path / "f.pptx")
    body = next(sh for sh in Presentation(out).slides[1].shapes if sh.name == "SUBRA text")
    assert body.top + body.height <= Inches(5.5)
    assert check_deck(out, report, get_theme("navy")).flagged_slides == []


# --- review fixes: agenda robustness, inherited footers --------------------

def _agenda_deck(tmp_path, items, title="Agenda"):
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Cover"
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = title
    tf = s.placeholders[1].text_frame
    tf.text = items[0]
    for item in items[1:]:
        tf.add_paragraph().text = item
    prs.save(tmp_path / "agenda.pptx")
    return tmp_path / "agenda.pptx"


def _agenda_plan(deck):
    ids = [t.id for t in deck.slides[1].texts if t.role != "title"]
    return parse_plan({"slides": [
        {"source_slide": 1, "layout": "title", "slots": {"title": ["s1-t1"]}},
        {"source_slide": 2, "layout": "agenda", "slots": {"title": ["s2-t1"], "items": ids}},
    ]})


@pytest.mark.parametrize("first", ["Where  we are", " Where we are", "Where\twe are", " Where we are"])
def test_agenda_survives_irregular_whitespace_in_its_items(tmp_path, first):
    src = _agenda_deck(tmp_path, [first, "Market", "Financials", "Plans"])
    deck = extract_deck(src)
    out, report = render_to(tmp_path, deck, _agenda_plan(deck), source=src)
    assert verify(deck, extract_deck(out)).passed and report.overflow_slides == []


def test_pre_numbered_agenda_items_are_not_numbered_again(tmp_path):
    from pptx.oxml.ns import qn

    src = _agenda_deck(tmp_path, ["1. Where we are", "2. Market", "3. Financials", "4. Plans"])
    deck = extract_deck(src)
    out, _ = render_to(tmp_path, deck, _agenda_plan(deck), source=src)
    box = next(sh for sh in Presentation(out).slides[1].shapes if sh.name == "SUBRA text")
    assert all(p._p.pPr is None or p._p.pPr.find(qn("a:buAutoNum")) is None for p in box.text_frame.paragraphs)
    assert verify(deck, extract_deck(out)).passed


def test_agenda_text_fits_beside_its_numbers(tmp_path):
    from polisher.render.textfit import FitPara, measure

    items = [f"Item {i}: a long agenda line about the quarter's work, its costs and the people involved" for i in range(7)]
    src = _agenda_deck(tmp_path, items)
    deck = extract_deck(src)
    out, report = render_to(tmp_path, deck, _agenda_plan(deck), source=src)
    assert report.overflow_slides == []
    box = next(sh for sh in Presentation(out).slides[1].shapes if sh.name == "SUBRA text")
    paragraphs = box.text_frame.paragraphs
    size = paragraphs[0].runs[0].font.size.pt
    mar_l = int(paragraphs[0]._p.pPr.get("marL")) / 12700
    paras = [FitPara(runs=[(p.text, False, False)]) for p in paragraphs]
    height = measure(paras, "Calibri", size, box.width / 12700 - mar_l, para_space_em=0.7).height
    assert height <= box.height / 12700


def test_footer_inherited_from_the_layout_is_avoided(tmp_path):
    from pptx.enum.shapes import PP_PLACEHOLDER

    from polisher.qa import check_deck

    prs = Presentation()
    layout = prs.slide_layouts[1]
    lf = next(p for p in layout.placeholders if p.placeholder_format.type == PP_PLACEHOLDER.FOOTER)
    lf.left, lf.top, lf.width, lf.height = Inches(0.5), Inches(5.5), Inches(9), Inches(0.4)
    prs.slides.add_slide(prs.slide_layouts[0]).shapes.title.text = "Cover"
    s = prs.slides.add_slide(layout)
    s.shapes.title.text = "Operations"
    tf = s.placeholders[1].text_frame
    tf.text = "Point one about the work we did this quarter"
    for i in range(9):
        tf.add_paragraph().text = f"Point {i + 2} about the work we did this quarter and what it cost"
    s.shapes.clone_placeholder(lf)
    footer = next(p for p in s.placeholders if p.placeholder_format.type == PP_PLACEHOLDER.FOOTER)
    footer.text = "Confidential"  # no position of its own: it inherits the layout's
    prs.save(tmp_path / "f.pptx")
    deck = extract_deck(tmp_path / "f.pptx")
    out, report = render_to(tmp_path, deck, plan_deck(deck), source=tmp_path / "f.pptx")
    body = next(sh for sh in Presentation(out).slides[1].shapes if sh.name == "SUBRA text")
    assert body.top + body.height <= Inches(5.5)
    assert check_deck(out, report, get_theme("navy")).flagged_slides == []
