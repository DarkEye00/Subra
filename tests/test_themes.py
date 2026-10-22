"""Every text colour a theme puts on a background must meet WCAG 4.5:1 (17 Oct build plan)."""

import pytest

from polisher.qa import contrast_ratio
from polisher.render.themes import THEMES

PAIRS = [("text", "bg"), ("muted", "bg"), ("title", "bg"), ("highlight", "bg"), ("text", "surface"),
         ("muted", "surface"), ("highlight", "surface"), ("on_primary", "primary"), ("on_primary_muted", "primary")]


@pytest.mark.parametrize("slug", sorted(THEMES))
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_theme_text_meets_contrast(slug, fg, bg):
    theme = THEMES[slug]
    assert contrast_ratio(getattr(theme, fg), getattr(theme, bg)) >= 4.5
