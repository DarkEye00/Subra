"""The layout sampler: every layout rendered with sample content in every theme, with no QA faults."""

import pytest

from polisher.extract import extract_deck
from polisher.layouts import LAYOUTS
from polisher.plan import parse_plan, validate_plan
from polisher.qa import check_deck
from polisher.render.renderer import render
from polisher.render.themes import THEMES, get_theme
from polisher.samples import build_sampler_deck, sampler_plan
from polisher.verify import verify


@pytest.mark.parametrize("theme", sorted(THEMES))
def test_sampler_renders_every_layout_without_faults(tmp_path, theme):
    src = build_sampler_deck(tmp_path / "sampler.pptx")
    deck = extract_deck(src)
    plan = parse_plan(sampler_plan(deck))
    assert validate_plan(plan, deck) == []
    assert [p.layout for p in plan.slides] == list(LAYOUTS)
    out = tmp_path / f"{theme}.pptx"
    report = render(plan, deck, get_theme(theme), out, src)
    assert verify(deck, extract_deck(out)).passed
    qa = check_deck(out, report, get_theme(theme))
    assert qa.flagged_slides == [], [(s.index, f.kind, f.detail) for s in qa.faulted for f in s.faults]


def test_render_layouts_script_writes_decks_and_an_index(tmp_path):
    from scripts.render_layouts import main

    assert main(["--out", str(tmp_path), "--no-png", "--themes", "navy"]) == 0
    assert (tmp_path / "navy.pptx").exists()
    html = (tmp_path / "index.html").read_text()
    assert "agenda" in html and "navy" in html
