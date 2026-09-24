import pytest

from polisher.render.textfit import FitPara, fit_size, measure, text_width

LONG = "Mobile money adoption keeps growing across every county we serve, " * 6


def para(text, level=0, bold=False, bullet=False):
    return FitPara(runs=[(text, bold, False)], level=level, bullet=bullet)


def test_width_scales_with_size_and_weight():
    w12 = text_width("Nairobi", "Calibri", 12)
    w24 = text_width("Nairobi", "Calibri", 24)
    assert w24 == pytest.approx(2 * w12, rel=0.01)
    assert text_width("Nairobi", "Calibri", 12, bold=True) > w12


def test_known_fonts_have_different_metrics():
    assert text_width("Nairobi", "Arial", 12) != text_width("Nairobi", "Calibri", 12)


def test_short_text_is_one_line():
    m = measure([para("Short line")], "Calibri", 20, width_pt=400)
    assert m.lines == 1


def test_long_text_wraps():
    m = measure([para(LONG)], "Calibri", 20, width_pt=400)
    assert m.lines > 3


def test_line_break_forces_a_new_line():
    assert measure([para("One\vTwo")], "Calibri", 20, width_pt=400).lines == 2


def test_unbreakable_word_is_split_by_characters():
    m = measure([para("x" * 300)], "Calibri", 20, width_pt=200)
    assert m.lines >= 3


def test_indentation_reduces_available_width():
    flat = measure([para(LONG)], "Calibri", 20, width_pt=400)
    deep = measure([para(LONG, level=3, bullet=True)], "Calibri", 20, width_pt=400)
    assert deep.lines > flat.lines


def test_paragraph_spacing_adds_height():
    one = measure([para("A")], "Calibri", 20, width_pt=400)
    two = measure([para("A"), para("B")], "Calibri", 20, width_pt=400)
    assert two.height > 2 * one.height


def test_fit_uses_max_size_when_text_is_short():
    r = fit_size([para("Hello")], "Calibri", 600, 200, max_pt=40, min_pt=12)
    assert (r.size, r.fits) == (40, True)


def test_fit_steps_down_for_long_text_and_fits():
    r = fit_size([para(LONG)], "Calibri", 500, 250, max_pt=40, min_pt=10)
    assert 10 <= r.size < 40
    assert r.fits
    assert measure([para(LONG)], "Calibri", r.size, 500).height <= 250


def test_fit_reports_overflow_at_min_size():
    r = fit_size([para(LONG * 10)], "Calibri", 200, 100, max_pt=24, min_pt=12)
    assert (r.size, r.fits) == (12, False)
