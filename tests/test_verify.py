import copy

import pytest

from deckbuilder import build_rich_deck
from polisher.extract import extract_deck
from polisher.model import Run, TextItem
from polisher.verify import extract_numbers, normalize_text, verify


@pytest.fixture(scope="module")
def original(tmp_path_factory):
    return extract_deck(build_rich_deck(tmp_path_factory.mktemp("v") / "rich.pptx"))


@pytest.fixture
def copy_of(original):
    return copy.deepcopy(original)


def problems_mentioning(report, needle):
    return [p for p in report.problems if needle in p]


# --- normalization --------------------------------------------------------

def test_normalize_collapses_whitespace_and_line_breaks():
    assert normalize_text("  Line one\vline two\n\tend ") == "Line one line two end"


@pytest.mark.parametrize(
    "text, numbers",
    [
        ("Revenue grew 45% to KES 12.5m", ["45%", "KES12.5m"]),
        ("1,200 units in 2026", ["1200", "2026"]),
        ("Churn: down to 3.2%", ["3.2%"]),
        ("$3bn and Ksh 400", ["$3bn", "KSH400"]),
        ("No numbers here", []),
        ("Q3", ["3"]),
        ("10km away, 5 million people", ["10", "5"]),
    ],
)
def test_extract_numbers(text, numbers):
    assert extract_numbers(text) == numbers


# --- verify ---------------------------------------------------------------

def test_identical_content_passes(original, copy_of):
    report = verify(original, copy_of)
    assert report.passed, report.problems
    assert report.problems == []


def test_missing_paragraph_fails(original, copy_of):
    copy_of.slides[1].texts.pop(1)
    report = verify(original, copy_of)
    assert not report.passed
    assert problems_mentioning(report, "Revenue grew 45% to KES 12.5m")
    assert problems_mentioning(report, "s2-t2")  # the original ID is named for the planner


def test_changed_number_fails_text_and_number_checks(original, copy_of):
    t = copy_of.slides[1].texts[1]
    t.text = t.text.replace("45%", "46%")
    report = verify(original, copy_of)
    assert not report.passed
    assert problems_mentioning(report, "46%")
    assert problems_mentioning(report, "Numbers")


def test_extra_text_fails(original, copy_of):
    copy_of.slides[0].texts.append(
        TextItem(id="x", slide=1, role="body", text="Invented claim", runs=[Run("Invented claim")])
    )
    report = verify(original, copy_of)
    assert not report.passed
    assert problems_mentioning(report, "Invented claim")


def test_line_break_and_whitespace_differences_pass(original, copy_of):
    t = copy_of.slides[1].texts[1]
    t.text = t.text.replace(" to ", "\vto  ")
    assert verify(original, copy_of).passed


def test_paragraphs_may_move_between_new_slides(original, copy_of):
    moved = copy_of.slides[1].texts.pop()
    copy_of.slides[2].texts.append(moved)
    assert verify(original, copy_of).passed


def test_changed_table_cell_fails(original, copy_of):
    copy_of.slides[2].tables[0].cells[1][1].text = "1,201"
    report = verify(original, copy_of)
    assert not report.passed
    assert problems_mentioning(report, "s3-tb1")


def test_changed_table_merge_fails(original, copy_of):
    cell = copy_of.slides[2].tables[0].cells[0][1]
    cell.col_span = 1
    report = verify(original, copy_of)
    assert not report.passed


def test_changed_chart_value_fails(original, copy_of):
    copy_of.slides[3].charts[0].series[0].values[2] = 14.26
    report = verify(original, copy_of)
    assert not report.passed
    assert problems_mentioning(report, "s4-c1")


def test_chart_values_tolerate_float_noise(original, copy_of):
    copy_of.slides[3].charts[0].series[0].values[2] = 14.25 + 1e-12
    assert verify(original, copy_of).passed


def test_changed_chart_title_fails(original, copy_of):
    copy_of.slides[3].charts[0].title = "Revenue"
    assert not verify(original, copy_of).passed


def test_missing_image_fails(original, copy_of):
    copy_of.slides[4].images.clear()
    report = verify(original, copy_of)
    assert not report.passed
    assert problems_mentioning(report, "s5-i1")


def test_changed_notes_fail(original, copy_of):
    copy_of.slides[1].notes = "Mention the Mombasa launch!"
    report = verify(original, copy_of)
    assert not report.passed
    assert problems_mentioning(report, "notes")


def test_notes_may_sit_on_a_split_slide_in_order(original, copy_of):
    # a split slide gives an extra slide with empty notes; order of non-empty notes is what matters
    from polisher.model import SlideContent

    copy_of.slides.insert(2, SlideContent(number=99))
    assert verify(original, copy_of).passed
