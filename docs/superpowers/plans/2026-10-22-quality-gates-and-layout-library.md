# Quality Gates and Layout Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the MVP up to the quality bar in "PPTX Polisher — Build Plan & Agent Prompt" (17 Oct 2026): no text below 12pt, no overlapping or overflowing shapes, a repair loop that re-plans only the slides that fail, a slide classifier, optional vision QA, a QA report for the customer and the admin, and the tooling (layout sample page, eval set runner) to keep it that way.

**Architecture:** The pipeline keeps its shape (validate, extract, plan, render in place, verify) and gains three stages the document asks for: a cheap classifier before planning, a deterministic QA pass over the rendered file after verification, and a repair loop that sends faulted slides back to the planner (twice at most), then falls back to the plainest layout and flags the slide. Vision QA sits behind a flag and feeds the same repair loop. The layout library grows from 9 to 12 layouts (agenda, timeline, image_full), each with item-count limits enforced by the plan validator; text capacity is enforced by real font metrics rather than character counts. Every stage reports tokens, cost and time.

**Tech Stack:** Python 3.14, python-pptx, lxml, Pillow, anthropic SDK (structured output via `output_config.format`, base64 image blocks), Django 5.2, Celery, pytest.

**Spec:** [PPTX Polisher — Build Plan & Agent Prompt.md](../../../PPTX%20Polisher%20—%20Build%20Plan%20&%20Agent%20Prompt.md) (the document) read against [deck-polisher-build-plan.md](../../../deck-polisher-build-plan.md) and its amendments (the MVP). Where they conflict, the amendments win on product decisions already shipped (Paystack, pay after generation, in-place rendering); the document wins on quality gates.

## Gap analysis (the document's stages against the MVP)

| Document | MVP today | This plan |
|---|---|---|
| 1 Parse with stable ids | `extract.py`, ids like `s3-t2` | No change |
| 2 Classify (cheap model, one batched call) | Missing; the planner classifies implicitly | Task 9: `classify.py`, Haiku, labels fed to the planner as hints; failure never fails the job |
| 3 Plan (strong model, splits never merges) | `planner.py`, whole deck in one call, structured output, prompt cached | Keep one call (whole-deck context, output is ~60 tokens a slide); add repair requests (Task 6) and the document's design rules |
| 4 Build native objects | `render/renderer.py` in place | Tasks 2 and 3: 12pt floor, three new layouts, shapes named for QA |
| 5 Render to PNG | `preview.py` after the pipeline | Task 10: rendered inside the pipeline when vision QA is on, and reused as the previews |
| 6 Check: deterministic then vision | Only a text-overflow flag | Task 4: `qa.py` (overflow, overlap, off-slide, tiny text, contrast, empty placeholders); Task 10: `vision.py` behind `VISION_QA` |
| 7 Repair flagged slides only, max 2, then fallback and flag | Whole-deck retry on verification failure only | Task 7: repair loop in `pipeline.py` with `planner.repair`, heuristic fallback, flags |
| 8 Verify data at 100% | `verify.py` | No change (chart series counts and table cells are already checked here) |
| 9 Deliver with a QA report | Attempts and an overflow list | Task 7 and 11: `qa_report` on the result and the order, notes on the preview page, admin |
| 16 layouts with capacity limits | 9 layouts, item limits, metric text fitting | 12 layouts (see mapping below); limits stay item counts, text capacity is measured not counted |
| Body 14 to 18pt, titles 28 to 36pt, never below 12pt | Body shrinks to 11pt, tables to 9pt | Task 2: 12pt floor everywhere; tables may reach 10pt only when a table can't fit at 12pt, and are flagged |
| Contrast 4.5:1 | Not checked | Task 4: theme test (all 27 pairs pass today) and a per-slide check |
| Log cost and time per stage | Per planner attempt | Task 7 and 11: timings per stage, `AIJob.stage` |
| Hard job timeout of 5 minutes | 30-minute stall sweep | Task 11: Celery soft limit 600s on generation (three planner attempts plus repairs need more than 5 minutes) |
| Eval set re-run after every change | `test-decks/` with three decks, run by hand | Task 13: `scripts/eval_decks.py` with a baseline comparison |
| Sample render page for every layout | None | Task 12: `polisher/samples.py` and `scripts/render_layouts.py` |
| Celery + Redis, stateless workers, LibreOffice in the worker, object storage, 7-day retention, rate limits, spend alerts | All present | No change |
| Cache planner output by content hash | Missing | Not built: theme switches already reuse the saved plan, and a re-upload is a new paid order |
| IntaSend at launch; preview 3 slides before charging | Paystack (Amendment 2); every slide is previewed before paying | No change: the shipped decisions are better on both counts |

Layout mapping (the document's 16 ids to the library's 12; names are unchanged because saved plans in the database reference them):

| Document id | Library layout |
|---|---|
| title, closing | `title` (closing slides may also use `quote`) |
| section | `section` |
| agenda | `agenda` (new, 3 to 7 items) |
| bullets-1col, bullets-2col | `bullets` (flows into two columns by itself when the list is long) |
| statement, quote | `quote` |
| kpi-3, kpi-4 | `stats` (1 to 4 cards) |
| comparison | `two_column` |
| table | `table` |
| chart-full, chart-text | `chart` (without or with `body`) |
| image-text | `image_text` |
| image-full | `image_full` (new, one visual, title band) |
| timeline | `timeline` (new, 3 to 6 steps) |

## Global Constraints

- Run tests from the repo root with `.venv/bin/python -m pytest -q -p no:cacheprovider`. Management commands take `--settings=subra.test_settings`.
- Never below 12pt: `renderer.MIN_PT = 12.0` is the floor for every text style. Tables try 18 down to 12; only when a table still doesn't fit at 12 may they go to `TABLE_FLOOR_PT = 10.0`, and the slide is flagged.
- Layout names in `polisher/layouts.py` never change (saved plans reference them). New names: `agenda`, `timeline`, `image_full`.
- Model ids: classifier and vision default to `claude-haiku-5-5` (vision-capable, $0.10 / $0.50 per million tokens, effort `low`). Planner and escalation defaults stay `claude-sonnet-5` and `claude-opus-5-5`.
- Every AI call goes through `AnthropicProvider.complete_json`, is preceded by `before_attempt(number, agent)` (the spend cap), and produces an `AttemptRecord` with a `stage` in `{"classify", "plan", "repair", "vision"}` passed to `on_attempt`.
- A classifier or vision failure (including an outage) never fails or defers the job; the planner's outage handling is unchanged.
- Repair loop: at most `MAX_REPAIR_PASSES = 2` planner repair passes, then the deterministic fallback, then the slide is flagged. A repair that fails verification or validation is discarded and the previous verified deck stays.
- Shapes the renderer draws are named `SUBRA decor`, `SUBRA card`, `SUBRA overlay`, `SUBRA text`, `SUBRA table`, `SUBRA chart` so QA can tell decoration from content.
- Customer copy (exact): `We re-laid out slide 4 so everything fits.` / `We re-laid out slides 4 and 9 so everything fits.`; `Slide 7 has more content than fits comfortably on one slide, so we used a simpler layout. Check it in PowerPoint.` (plural: `Slides 7 and 8 have more content than fits comfortably on one slide, so we used simpler layouts. Check them in PowerPoint.`).
- Commits: imperative sentence, no prefix, ending with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Code style: match the repo (sparse "why" comments, 120-char lines, British English in UI text).

## Review Focus

Failure modes the document implies but its own test list doesn't cover; each has a test in the owning task:

1. A master whose footer sits inside the content area: body text must shrink above it, not overlap it (Task 4, `test_footer_in_content_area_is_avoided`).
2. A repair whose re-render fails verification must leave the previous verified deck untouched (Task 7, `test_repair_that_breaks_verification_is_discarded`).
3. A 30-row table that can't fit at 12pt must still arrive complete, at 10pt or larger, and be flagged (Task 2, `test_long_table_floors_at_ten_points_and_is_flagged`).
4. A classifier or vision outage must not fail or defer a deck the planner can finish (Task 9 `test_classifier_outage_does_not_fail_the_job`, Task 10 `test_vision_outage_does_not_fail_the_job`).
5. Agenda numbering must add no text or numbers the original lacks, so retention stays at 100% (Task 3, `test_agenda_numbers_are_bullets_not_text`).

---

### Task 1: Spec into the repo

**Files:**
- Add: `PPTX Polisher — Build Plan & Agent Prompt.md` (already in the working tree, untracked)
- Add: `docs/superpowers/plans/2026-10-22-quality-gates-and-layout-library.md` (this file)
- Modify: `.gitignore` (the `test-decks/` line is already staged in the working tree)

- [ ] **Step 1: Commit**

```bash
git add ".gitignore" "PPTX Polisher — Build Plan & Agent Prompt.md" docs/superpowers/plans/2026-10-22-quality-gates-and-layout-library.md
git commit -m "Add the 17 Oct polisher build plan and the plan to close its gaps"
```

---

### Task 2: 12pt floor and named shapes in the renderer

**Files:**
- Modify: `polisher/render/renderer.py` (`_body_style` calls with 11, `_layout_stats` label style, `_table`, `_rect`, `_card`, `_text`, `_write_stat`, `_chart`)
- Test: `tests/test_render.py`

**Interfaces:**
- Produces: `renderer.MIN_PT = 12.0`, `renderer.TABLE_FLOOR_PT = 10.0`; `_rect(self, slide, box, color, shape=MSO_SHAPE.RECTANGLE, radius=None, name="SUBRA decor")`; drawn shape names as in Global Constraints. `RenderReport.slides[i]` gains `"min_pt": float` (the smallest font size the renderer used on that slide).

- [ ] **Step 1: Write the failing tests**

```python
def _run_sizes(prs, in_tables):
    """All explicit run sizes (pt) on every slide, inside or outside tables."""

def test_no_text_below_twelve_points_on_the_business_deck(tmp_path):
    # build_business_deck → heuristic plan → render in every theme
    assert min(_run_sizes(prs, in_tables=False)) >= 12

def test_long_table_floors_at_ten_points_and_is_flagged(tmp_path):
    # a 30-row, 4-column table on one slide
    assert 10 <= min(_run_sizes(prs, in_tables=True)) < 12
    assert report.overflow_slides == [1]
    assert report.slides[0]["min_pt"] < 12
    assert verify(deck, extract_deck(out)).passed

def test_drawn_shapes_are_named(tmp_path, rich):
    names = {sh.name for slide in prs.slides for sh in slide.shapes}
    assert {"SUBRA decor", "SUBRA card", "SUBRA text", "SUBRA table", "SUBRA chart"} <= names
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_render.py -q -k "twelve or floors or named"`
Expected: FAIL (sizes of 11 and 9 appear; names are python-pptx defaults).

- [ ] **Step 3: Implement**

In `renderer.py`: add the two constants; replace every `11` minimum with `MIN_PT`; in `_table` iterate `range(18, 11, -1)` for a fit, else `range(11, 9, -1)` setting `self.overflow = True` whenever the chosen size is below `MIN_PT` or nothing fits; track `self.min_pt = min(self.min_pt, size)` in `_text`, `_write_stat`, `_table`, reset per slide in `render()` and written to the report entry; set `.name` on every shape the renderer adds.

- [ ] **Step 4: Run the whole render suite**

Run: `.venv/bin/python -m pytest tests/test_render.py tests/test_render_inplace.py tests/test_pipeline.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Keep text at 12pt or larger and name the shapes the renderer draws"
```

---

### Task 3: Agenda, timeline and image_full layouts

**Files:**
- Modify: `polisher/layouts.py`, `polisher/render/renderer.py`, `polisher/heuristic.py`, `polisher/planner.py` (design guidance only)
- Test: `tests/test_layouts.py` (new), `tests/test_render.py`, `tests/test_heuristic.py`, `tests/test_planner.py`

**Interfaces:**
- Produces in `layouts.LAYOUTS`:
  - `agenda`: slots `TITLE`, `Slot("items", "text", ..., min_items=3, max_items=7)`
  - `timeline`: slots `TITLE`, `Slot("steps", "text", ..., min_items=3, max_items=6)`
  - `image_full`: slots `TITLE`, `Slot("visuals", "visual", ..., min_items=1, max_items=1)`
- `heuristic.plan_items` picks `agenda` when the title matches `AGENDA_TITLE = re.compile(r"\b(agenda|contents|outline|overview|what we.ll cover)\b", re.I)` and there are 3 to 7 body paragraphs at level 0; `image_full` when the slide holds exactly one `image` item, no body text, and its area (`pos[2] * pos[3]`) is at least 0.4.
- `plan.TEXT_SLOT_PREFERENCE` gains `"items"`, `"steps"`; `plan._enforce_limits` falls back to `bullets` when `items`/`steps` has fewer than the minimum (existing branch) and splits extras onto a `bullets` slide (existing branch).
- Renderer methods `_layout_agenda`, `_layout_timeline`, `_layout_image_full`.

- [ ] **Step 1: Write the failing tests**

`tests/test_layouts.py`:
```python
def test_library_has_twelve_layouts():
    assert sorted(LAYOUTS) == ["agenda", "bullets", "chart", "image_full", "image_text", "quote", "section",
                               "stats", "table", "timeline", "title", "two_column"]

@pytest.mark.parametrize("layout,slot,lo,hi", [("agenda", "items", 3, 7), ("timeline", "steps", 3, 6),
                                                ("image_full", "visuals", 1, 1), ("stats", "stats", 1, 4)])
def test_capacity_limits_are_declared(layout, slot, lo, hi):
    s = LAYOUTS[layout].slot(slot)
    assert (s.min_items, s.max_items) == (lo, hi)

def test_agenda_with_two_items_is_invalid_and_repairs_to_bullets(deck):  # business deck slide 2 has 5 items
    # plan slide 2 as agenda with items ["s2-t2", "s2-t3"] and the rest in a second bullets part
    assert_error(validate_plan(plan, deck), "items", "at least 3")
    repaired, _ = repair_plan(plan, deck)
    assert repaired.slides[1].layout == "bullets"

def test_agenda_with_eight_items_splits(deck8):
    assert_error(validate_plan(plan, deck8), "items", "at most 7")
    repaired, _ = repair_plan(plan, deck8); assert validate_plan(repaired, deck8) == []
```

`tests/test_render.py`:
```python
@pytest.mark.parametrize("theme", sorted(THEMES))
def test_new_layouts_round_trip(tmp_path, theme):
    # build_business_deck: slide 2 → agenda, slide 12 → timeline of its 4 steps + title? (12 has 4 numbered items) ,
    # slide 9 (Mombasa hub picture + bullets) → image_full with visuals only is invalid (body text exists), so use a
    # custom deck: one slide with a single large picture and a title → image_full
    assert verify(deck, extract_deck(out)).passed

def test_agenda_numbers_are_bullets_not_text(tmp_path):
    # agenda items carry a:buAutoNum and the extracted output has no text or number the source lacks
    assert all(p._p.pPr.find(qn("a:buAutoNum")) is not None for p in items_paragraphs)
    assert verify(deck, extract_deck(out)).passed

def test_image_full_picture_fills_most_of_the_slide(tmp_path):
    assert pic.width * pic.height >= 0.5 * prs.slide_width * prs.slide_height

def test_timeline_draws_one_marker_per_step(tmp_path):
    ovals = [sh for sh in prs.slides[0].shapes if sh.name == "SUBRA decor" and sh.auto_shape_type == MSO_SHAPE.OVAL]
    assert len(ovals) == 4
```

`tests/test_heuristic.py`:
```python
def test_agenda_slide_gets_the_agenda_layout(tmp_path):  # build_business_deck slide 2
    assert plan.slides[1].layout == "agenda" and len(plan.slides[1].slots["items"]) == 5

def test_single_large_picture_gets_image_full(tmp_path):
```

`tests/test_planner.py`:
```python
def test_system_prompt_describes_the_new_layouts():
    assert "### agenda" in SYSTEM_PROMPT and "### timeline" in SYSTEM_PROMPT and "### image_full" in SYSTEM_PROMPT
    assert "three times in a row" in SYSTEM_PROMPT
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_layouts.py tests/test_render.py tests/test_heuristic.py tests/test_planner.py -q`
Expected: FAIL (unknown layouts).

- [ ] **Step 3: Implement the layouts**

`layouts.py`: three `Layout` entries with descriptions the planner reads (agenda: "Contents or outline: 3 to 7 numbered items, one per paragraph, shown large with accent numbers"; timeline: "3 to 6 steps or dates on a horizontal line, one paragraph per node"; image_full: "One photo or visual fills the slide, with the title on a band at the bottom. No body text").

`renderer.py`:
- `_layout_agenda`: `_content_slide`, then one text box over the content area with the items as paragraphs: `TStyle(t.body_font, t.text, 28, MIN_PT, bullets=False, para_space_em=0.7)`; after `_text`, set on each paragraph `a:buFont typeface=heading_font`, `a:buClr` accent, `a:buSzPct val=150000`, `a:buAutoNum type="arabicPeriod"`, `marL` 0.75in and matching negative `indent` (a helper `_agenda_numbering(p, size, color, font)` next to `_set_bullet`). Numbers are bullets, never text runs.
- `_layout_timeline`: `_content_slide`; `n = len(steps)`; `colw = area.w / n`; a horizontal `_rect` line `Box(area.x + colw/2, area.y + 0.9, colw*(n-1), 0.06)` in `t.line`; one oval per step `Box(cx - 0.18, area.y + 0.72, 0.36, 0.36)` in `t.accent`; label box per step `Box(area.x + i*colw + 0.1, area.y + 1.35, colw - 0.2, area.h - 1.5)` with `TStyle(t.body_font, t.text, 20, MIN_PT, bullets=False, align=PP_ALIGN.CENTER)`; one size for all labels (min of the fits, as `_layout_two_column` does).
- `_layout_image_full`: `_take_slide(t.bg)`; place the single visual in `Box(0, 0, W, CANVAS_H - 1.3)` via `_place_visuals`; band `_rect(Box(0, CANVAS_H - 1.3, W, 1.3), t.primary, name="SUBRA overlay")` drawn after the picture (so it stays in front: skip the move-to-back step when `name == "SUBRA overlay"`); title text in the band `TStyle(t.heading_font, t.on_primary, 32, 20, bold=t.heading_bold, override_runs=True, anchor=MSO_ANCHOR.MIDDLE, bullets=False)` using `_title_shape()`.

`heuristic.py`: the two rules from Interfaces, applied before the `stats`/`two_column` checks and after the visuals branch (image_full is a visuals case: one image, no texts besides the title).

`planner.py` design guidance (verbatim lines added under "## Design guidance"):
- `- `agenda` suits a contents or outline slide with 3 to 7 items; `timeline` suits 3 to 6 dated or sequential steps; `image_full` suits a slide that is one photo plus a title and nothing else.`
- `- A bullets slide with 1 to 3 short, punchy lines reads better as `stats` (when they are figures) or `quote` (when it is one message).`
- `- Vary consecutive slides: avoid the same layout three times in a row unless the content demands it.`

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (the `test_heuristic_plan_for_rich_deck_is_valid` layout list stays the same; the rich deck has no agenda slide).

- [ ] **Step 5: Commit**

```bash
git commit -am "Add agenda, timeline and image_full layouts"
```

---

### Task 4: Deterministic QA checks

**Files:**
- Create: `polisher/qa.py`
- Modify: `polisher/render/renderer.py` (`_zones` adds footer placeholder boxes with text)
- Test: `tests/test_qa.py` (new), `tests/test_themes.py` (new), `tests/test_render.py`

**Interfaces:**
- Produces (`polisher/qa.py`):
  ```python
  MIN_FONT_PT = 12.0; MIN_CONTRAST = 4.5; OVERLAP_TOLERANCE = 0.02
  @dataclass
  class Fault: kind: str; detail: str; repairable: bool = True
      # kind in overflow | overlap | off_slide | tiny_text | low_contrast | empty_placeholder | vision
  @dataclass
  class SlideQA: index: int; source_slide: int; layout: str; faults: list = field(default_factory=list)
      repaired: bool = False; fallback: bool = False; vision_score: float | None = None
      ok -> bool; to_dict() -> dict
  @dataclass
  class QAReport: slides: list[SlideQA]
      faulted -> list[SlideQA]; flagged_slides -> list[int]; repairable_sources -> list[int]
      faults_by_source() -> dict[int, list[str]]   # "Planned slide 3 (layout bullets): text overflows ..." lines
      mark(repaired_sources=(), fallback_sources=()) -> None
      to_dict() -> dict
  def contrast_ratio(hex_a: str, hex_b: str) -> float          # WCAG 2.x relative luminance
  def check_deck(pptx_path, render_report, theme) -> QAReport  # one SlideQA per render_report.slides entry
  ```
- Checks per slide, on the saved file's XML (`extract.top_shapes`, `inplace.element_box`):
  - `overflow`: `render_report.slides[i]["overflow"]` is true. Detail: `"text does not fit its box even at 12pt"` (or `"table text is below 12pt"` when `min_pt < 12`).
  - `overlap`: pairwise content boxes (text-bearing `p:sp`, `p:pic`, `p:graphicFrame`, `mc:AlternateContent`, `p:contentPart`; skip names `SUBRA decor` and `SUBRA card`; `SUBRA overlay` may overlap pictures). Fault when intersection area exceeds `OVERLAP_TOLERANCE` of the smaller box. Detail names both shapes by their first words of text or kind, e.g. `"'Regional sales' table overlaps 'Figures are unaudited' text"`.
  - `off_slide`: any content box (not decor) beyond the slide bounds by more than 0.02in.
  - `tiny_text`: any `a:rPr/@sz` below 1200 in the slide XML (table cells included). Not repairable when the only tiny text is inside a table and the slide's plan has one item.
  - `low_contrast`: each text run's `a:srgbClr` against the fill of the topmost `SUBRA card`/`SUBRA overlay`/`SUBRA decor` rect containing the text box centre, else the slide's solid background, else `theme.bg`. Not repairable.
  - `empty_placeholder`: a `p:sp` with `p:ph` and no `a:t` text. Repairable.
- Renderer: `_zones` also returns the boxes of kept footer placeholders (`ftr`, `dt`) that contain text, so `_avoid` keeps content off them.

- [ ] **Step 1: Write the failing tests**

`tests/test_themes.py`:
```python
PAIRS = [("text", "bg"), ("muted", "bg"), ("title", "bg"), ("highlight", "bg"), ("text", "surface"), ("muted", "surface"),
         ("highlight", "surface"), ("on_primary", "primary"), ("on_primary_muted", "primary")]
@pytest.mark.parametrize("slug", sorted(THEMES))
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_theme_text_meets_contrast(slug, fg, bg):
    assert contrast_ratio(getattr(THEMES[slug], fg), getattr(THEMES[slug], bg)) >= 4.5
```

`tests/test_qa.py`:
```python
def test_contrast_ratio_matches_wcag_reference():
    assert contrast_ratio("000000", "FFFFFF") == pytest.approx(21.0)
    assert contrast_ratio("777777", "FFFFFF") == pytest.approx(4.48, abs=0.01)

@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize("builder", [build_rich_deck, build_business_deck])
def test_clean_render_has_no_faults(tmp_path, theme, builder):
    qa = check_deck(out, report, get_theme(theme))
    assert qa.flagged_slides == [], [f.detail for s in qa.faulted for f in s.faults]

def test_overlapping_text_boxes_are_reported(tmp_path):  # hand-built pptx with two text boxes at the same place
    assert [f.kind for f in qa.slides[0].faults] == ["overlap"]

def test_decoration_and_cards_do_not_count_as_overlap(tmp_path):  # a "SUBRA card" rect under a text box

def test_tiny_text_is_reported(tmp_path):  # a 9pt run → kind "tiny_text", detail contains "9pt"

def test_shape_off_the_slide_is_reported(tmp_path):  # a text box whose bottom is 1in below the slide

def test_low_contrast_is_reported(tmp_path):  # light grey text on white → "low_contrast", repairable False

def test_empty_placeholder_is_reported(tmp_path):

def test_overflow_flag_from_the_render_report_is_a_fault(tmp_path):  # the 400-word slide from test_render

def test_faults_by_source_groups_descriptions_for_the_planner(tmp_path):
    assert faults == {1: ["Planned slide 1 (layout bullets): text does not fit its box even at 12pt"]}

def test_mark_sets_repaired_and_fallback_flags():
```

`tests/test_render.py`:
```python
def test_footer_in_content_area_is_avoided(tmp_path):
    # a footer placeholder moved to y = 5.5in with the text "Confidential"; after rendering, the body text box bottom
    # is above the footer's top and check_deck reports no overlap
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_qa.py tests/test_themes.py tests/test_render.py -q`
Expected: FAIL with `ModuleNotFoundError: polisher.qa`.

- [ ] **Step 3: Implement `polisher/qa.py` and the footer zones**

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS. If `test_clean_render_has_no_faults` finds a real fault in a layout, fix the layout, not the test.

- [ ] **Step 5: Commit**

```bash
git add polisher/qa.py tests/test_qa.py tests/test_themes.py
git commit -am "Check rendered slides for overlap, overflow, tiny text and contrast"
```

---

### Task 5: Plan splicing and heuristic fallback plans

**Files:**
- Modify: `polisher/plan.py`, `polisher/heuristic.py`
- Test: `tests/test_plan.py`, `tests/test_heuristic.py`

**Interfaces:**
- `plan.replace_source(plan, source_slide, new_slides) -> Plan` returns a copy with every planned slide of `source_slide` replaced, in place, by `new_slides` (all with that `source_slide`, else `ValueError`).
- `heuristic.plan_items(source_slide, items, is_first_slide=False, is_last_slide=False, max_body_chars=MAX_BODY_CHARS, max_body_paragraphs=MAX_BODY_PARAGRAPHS)`.
- `heuristic.fallback_plan(source_slide, items, level=1) -> list[PlannedSlide]`: the plainest safe plan. Visuals (locked first) go on their own `table`/`chart`/`image_text` slide first, with the title; text goes in `bullets` chunks of at most `MAX_BODY_CHARS / 2**level` characters and `max(2, MAX_BODY_PARAGRAPHS // 2**level)` paragraphs; the first text chunk takes the title when there are no visuals. Never `stats`, `two_column`, `agenda`, `timeline` or `quote`.

- [ ] **Step 1: Write the failing tests**

```python
def test_replace_source_swaps_a_slides_parts_in_place(deck):
    plan = parse_plan(VALID)
    new = replace_source(plan, 2, [PlannedSlide(2, "bullets", {"title": ["s2-t1"], "body": ["s2-t2"]}),
                                   PlannedSlide(2, "bullets", {"body": ["s2-t3", "s2-t4"]})])
    assert [p.source_slide for p in new.slides] == [1, 2, 2, 3, 4, 5, 6, 7]
    assert plan.slides[1].slots["body"] == ["s2-t2", "s2-t3", "s2-t4"]  # the original is untouched

def test_replace_source_rejects_slides_from_another_source(deck):
    with pytest.raises(ValueError): replace_source(plan, 2, [PlannedSlide(3, "bullets", {})])

def test_fallback_plan_splits_text_in_halves_then_quarters(tmp_path):  # the 12-point "Everything" slide
    assert len(fallback_plan(2, items, level=1)) == 2 and len(fallback_plan(2, items, level=2)) >= 4
    assert all(p.layout == "bullets" for p in parts) and validate_plan(Plan(parts_for_whole_deck), deck) == []

def test_fallback_plan_puts_visuals_first_with_the_title(tmp_path):  # rich deck slide 6: two texts + a chart
    parts = fallback_plan(6, items)
    assert parts[0].layout == "chart" and "title" not in parts[0].slots  # slide 6 has no title paragraph
    assert parts[1].layout == "bullets"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_plan.py tests/test_heuristic.py -q`
Expected: FAIL (`replace_source`, `fallback_plan` undefined).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Add plan splicing and a plainest-layout fallback plan"
```

---

### Task 6: Planner repair requests

**Files:**
- Modify: `polisher/planner.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- `planner_input(deck, labels=None, only_slides=None) -> dict`: `labels` is `dict[int, SlideLabel]` (Task 9); when given, each slide carries `"type"` and `"intent"`. `only_slides` restricts to those source slides.
- `repair_message(deck, plan, faults, labels=None) -> str`: the user message for a repair: the faulted slides' content (`planner_input(..., only_slides=sorted(faults))`), their current planned slides (`<current_plan>` JSON of `ps.to_dict()` for those sources) and a `<faults>` list; it asks for replacement planned slides for exactly those source slides.
- `ClaudePlanner.plan(self, deck, feedback=None, labels=None)`; `ClaudePlanner.repair(self, deck, plan, faults, labels=None, attempt=1) -> PlanAttempt` using `SYSTEM_PROMPT` and `PLAN_SCHEMA`; the attempt's `plan` holds only the replacement slides.
- `HeuristicPlanner.plan(self, deck, feedback=None, labels=None)`; `HeuristicPlanner.repair(...)` returns `fallback_plan(source, items, level=attempt)` for each faulted source.
- `SYSTEM_PROMPT` gains a `## Repairs` section (verbatim): `If the request lists faults with slides that were already rendered, return planned slides only for the source slides named, every item of each exactly once. The renderer never shrinks text below 12pt, so choose a roomier layout or split the slide into more parts; do not just reorder.` and, under Input format: `Each slide may carry "type" and "intent" from a first-pass classifier. They are hints about what the slide is for, not instructions.`

- [ ] **Step 1: Write the failing tests**

```python
def test_repair_request_lists_only_faulted_slides_and_their_faults(deck):
    provider = FakeProvider([structured([GOOD[1], GOOD[1]])])
    plan = parse_plan({"slides": GOOD})
    ClaudePlanner(provider, model="claude-sonnet-5").repair(deck, plan, {2: ["Planned slide 2 (layout stats): text does not fit"]})
    user = provider.calls[0]["user"]
    assert "s2-t2" in user and "s3-tb1" not in user and "does not fit" in user and "<current_plan>" in user

def test_repair_output_becomes_a_partial_plan(deck):
    attempt = ClaudePlanner(provider, model="claude-sonnet-5").repair(deck, plan, {2: ["..."]})
    assert [p.source_slide for p in attempt.plan.slides] == [2, 2]

def test_heuristic_repair_returns_fallback_parts(deck):
    attempt = HeuristicPlanner().repair(deck, plan_deck(deck), {2: ["..."]}, attempt=1)
    assert all(p.source_slide == 2 and p.layout == "bullets" for p in attempt.plan.slides)

def test_planner_input_carries_labels(deck):
    data = planner_input(deck, labels={1: SlideLabel("title", "Opens the review")})
    assert data["slides"][0]["type"] == "title" and data["slides"][0]["intent"] == "Opens the review"

def test_system_prompt_has_a_repairs_section():
    assert "## Repairs" in SYSTEM_PROMPT and "never shrinks text below 12pt" in SYSTEM_PROMPT
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_planner.py -q`
Expected: FAIL (`repair` missing; `SlideLabel` import from `polisher.classify` fails until Task 9: define `SlideLabel` in `polisher/classify.py` now as a bare dataclass so this task can import it).

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add polisher/classify.py
git commit -am "Let the planner re-plan only the slides that failed layout checks"
```

---

### Task 7: Repair loop, QA report and timings in the pipeline and CLI

**Files:**
- Modify: `polisher/pipeline.py`, `polisher/cli.py`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- `AttemptRecord` gains `stage: str = "plan"`.
- `PolishResult` gains `qa: QAReport | None = None`, `final_plan: dict | None = None`, `labels: dict | None = None`, `timings_ms: dict = field(default_factory=dict)`, `previews: list = field(default_factory=list)`, `repair_passes: int = 0`. Properties: `plan -> final_plan`; `overflow_slides -> qa.flagged_slides` (empty without `qa`); `qa_report -> dict` with keys `retention` (`{"passed", "texts", "tables", "charts", "images", "locked"}`), `slides` (`SlideQA.to_dict()` list), `repaired_slides`, `fallback_slides`, `flagged_slides`, `repair_passes`, `vision_qa` (bool), `labels` (`{slide: {"type", "intent"}}` or `None`), `timings_ms`, `cost_usd` (`{"classify", "plan", "repair", "vision", "total"}`).
- `polish(input_path, output_path, theme, planner, escalation=None, render_fn=render, before_attempt=None, on_attempt=None, classifier=None, vision=None, preview_dir=None, preview_fn=None, max_repair_passes=MAX_REPAIR_PASSES)`. `classifier`, `vision`, `preview_dir`, `preview_fn` are wired in Tasks 9 and 10 but accepted now.
- Flow after the first verified deck: `qa = check_deck(output, report, theme)`; while `qa.repairable_sources` and passes < max: `before_attempt`, `planner.repair(deck, plan, qa.faults_by_source(), labels, attempt=passes)`, splice each returned source with `replace_source`, `repair_plan`, `validate_plan`, render to a temp file, verify, `check_deck`; accept (replace output, plan, report, qa; `qa.mark(repaired=...)`) when verification passes and the new report has no more faulted sources than before; otherwise keep the previous deck and stop. Then for each remaining repairable source: `fallback_plan(level=2)` spliced the same way, accepted on the same terms, `qa.mark(fallback_sources=...)`. Repair records: `stage="repair"`, `problems` = the fault lines, `plan` = the accepted plan dict, `verification_passed`.
- Timings (ms): `extract`, `classify`, `plan` (every planner attempt including its render and verify), `repair`, `qa`, `vision`, `previews`, `total`.
- `rerender(...)` also runs `check_deck`; `RerenderResult` gains `qa` and its `overflow_slides` reads from it.
- CLI: `--repair-passes N` (default 2); prints each attempt with its stage, then `QA: repaired [..], fallbacks [..], flagged [..]` and `timings: extract 120ms plan 3400ms ...`; `--report` JSON gains `"qa"` and `"timings_ms"`.

- [ ] **Step 1: Write the failing tests**

```python
class OverflowThenFixPlanner(ScriptedPlanner):
    """First plan crams a long slide into one bullets part; repair() returns the heuristic fallback."""

def _long_deck(tmp_path):  # cover + one slide with 12 long paragraphs (as in test_heuristic)

def test_overflowing_slide_is_repaired_by_the_planner(tmp_path):
    result = polish(src, out, theme="navy", planner=OverflowThenFixPlanner())
    assert result.ok and result.repair_passes == 1
    assert [a.stage for a in result.attempts] == ["plan", "repair"]
    assert result.qa_report["repaired_slides"] and result.qa_report["flagged_slides"] == []
    assert len(result.plan["slides"]) > 2

def test_two_failed_repairs_fall_back_and_flag_the_slide(tmp_path):
    # repair() keeps returning the same cramped single part
    assert result.repair_passes == 2
    assert result.qa_report["fallback_slides"] and verify(...).passed

def test_heuristic_planner_repairs_by_splitting(tmp_path):
    result = polish(src, out, theme="navy", planner=HeuristicPlanner())  # heuristic chunks at 1100 chars, so force overflow
    # with a deck whose single paragraph is 2,000 characters: not splittable → flagged, never crashes
    assert result.ok and result.qa_report["flagged_slides"] == [2]

def test_repair_that_breaks_verification_is_discarded(tmp_path):
    # render_fn drops a picture on the second render only
    assert result.ok and result.attempts[1].stage == "repair" and result.attempts[1].verification_passed is False
    assert verify(deck, extract_deck(out)).passed and result.repair_passes == 1

def test_qa_report_shape_and_timings(tmp_path, deck_path):
    report = result.qa_report
    assert report["retention"]["passed"] and report["retention"]["texts"] == 12
    assert set(report["timings_ms"]) >= {"extract", "plan", "qa", "total"} and report["vision_qa"] is False
    assert set(report["cost_usd"]) == {"classify", "plan", "repair", "vision", "total"}

def test_before_attempt_runs_for_repairs_too(tmp_path):
    assert seen == [1, 2]  # one plan, one repair

def test_rerender_reports_qa(tmp_path, deck_path):
    assert result.qa is not None and result.overflow_slides == []
```

`tests/test_cli.py`:
```python
def test_cli_report_includes_qa_and_timings(tmp_path, capsys):
    assert data["qa"]["retention"]["passed"] is True and "total" in data["timings_ms"]
    assert "QA:" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py tests/test_cli.py -q`
Expected: FAIL (`repair_passes`, `qa_report` missing).

- [ ] **Step 3: Implement**

Split `_attempt` so rendering and verifying a plan is its own function `_render_verified(plan, deck, theme, input_path, output_path, render_fn) -> (report, verification, tmp_path)` used by both the planner attempts and the repair passes. Keep `PlannerUnavailable` behaviour for planner attempts; a `retryable` repair error ends the repair loop (the deck is already verified) and is logged, not raised.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git commit -am "Repair slides that fail layout checks, then fall back and flag them"
```

---

### Task 8: Images in the provider, current model prices

**Files:**
- Modify: `polisher/ai.py`, `polisher/cost.py`
- Test: `tests/test_planner.py`

**Interfaces:**
- `AnthropicProvider.complete_json(self, *, model, system, user, schema, effort, max_tokens=64000, images=None)`: `images` is a list of PNG bytes; when given, the user message content is `[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": <b64>}} ...] + [{"type": "text", "text": user}]`.
- `cost.PRICING` gains `"claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}` and `"claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_read": 0.20, "cache_write": 2.50}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_anthropic_provider_sends_images_as_base64_blocks():
    AnthropicProvider(client=client).complete_json(model="m", system="s", user="look", schema={}, effort="low", images=[b"\x89PNG"])
    content = client.kwargs["messages"][0]["content"]
    assert content[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(b"\x89PNG").decode()}}
    assert content[-1] == {"type": "text", "text": "look"}

def test_cost_table_prices_haiku_5_5():
    assert cost_usd("claude-haiku-5-5", Usage(1_000_000, 1_000_000)) == pytest.approx(0.60)
```

- [ ] **Step 2: Run to verify they fail**, **Step 3: Implement**, **Step 4: Run `tests/test_planner.py`**

- [ ] **Step 5: Commit**

```bash
git commit -am "Accept images in AI requests and price Haiku 5.5"
```

---

### Task 9: Slide classifier

**Files:**
- Modify: `polisher/classify.py` (created bare in Task 6), `polisher/pipeline.py`, `polisher/cli.py`
- Test: `tests/test_classify.py` (new), `tests/test_pipeline.py`

**Interfaces:**
```python
SLIDE_TYPES = ("title", "section", "agenda", "bullets", "statement", "kpi", "comparison", "table", "chart",
               "image-led", "timeline", "quote", "closing", "mixed")
@dataclass
class SlideLabel: type: str; intent: str
@dataclass
class ClassifyAttempt: labels: dict | None; model: str | None; usage: Usage; cost_usd: float; duration_ms: int
    error: str | None = None; retryable: bool = False
CLASSIFIER_PROMPT: str   # the document's classifier prompt, with the type list from SLIDE_TYPES and "Return only JSON matching the schema"
CLASSIFY_SCHEMA: dict    # {"slides": [{"slide": int, "type": enum SLIDE_TYPES, "intent": str}]}, additionalProperties False everywhere
def classifier_input(deck) -> list[dict]
    # per slide: {"slide", "title", "text": first 12 paragraphs cut to 120 chars, "tables", "charts", "images", "locked"}
class ClaudeClassifier:
    name = "claude"
    def __init__(self, provider, model="claude-haiku-5-5", effort="low", pricing=None)
    def classify(self, deck) -> ClassifyAttempt      # labels for every slide the model returned; missing slides get no label
class HeuristicClassifier:
    name = "heuristic"; model = None
    def classify(self, deck) -> ClassifyAttempt      # first slide title, last short slide closing, tables table, charts chart, images image-led, agenda by AGENDA_TITLE, otherwise bullets; intent ""
```
- Pipeline: when `classifier` is given, `before_attempt(0, classifier)`, `classify`, record `stage="classify"` (number 0), `on_attempt`; `labels` go to `planner.plan(deck, feedback, labels=labels)` and `planner.repair(..., labels=labels)`. An error leaves `labels=None`.
- CLI: `--classifier {on,off}` default `on`; with `--planner heuristic` the heuristic classifier is used (free), otherwise `ClaudeClassifier(provider, os.environ.get("CLASSIFIER_MODEL", "claude-haiku-5-5"))`.

- [ ] **Step 1: Write the failing tests**

```python
def test_classifier_input_has_every_slide_and_no_binaries(deck):
    data = classifier_input(deck); assert [s["slide"] for s in data] == [1, 2, 3, 4, 5, 6, 7]; assert "blob" not in json.dumps(data)

def test_claude_classifier_returns_labels(deck):
    provider = FakeProvider([{"slides": [{"slide": 1, "type": "title", "intent": "Opens the review"}]}])
    attempt = ClaudeClassifier(provider).classify(deck)
    assert attempt.labels[1] == SlideLabel("title", "Opens the review") and attempt.error is None
    assert provider.calls[0]["model"] == "claude-haiku-5-5" and provider.calls[0]["effort"] == "low"

def test_classifier_error_returns_no_labels(deck):
    attempt = ClaudeClassifier(FakeProvider([ProviderError("down", retryable=True)])).classify(deck)
    assert attempt.labels is None and attempt.retryable

def test_heuristic_classifier_labels_the_rich_deck(deck):
    labels = HeuristicClassifier().classify(deck).labels
    assert labels[1].type == "title" and labels[3].type == "table" and labels[4].type == "chart"

def test_schema_forbids_additional_properties():  # walk CLASSIFY_SCHEMA as test_planner does
```

`tests/test_pipeline.py`:
```python
def test_classifier_labels_reach_the_planner(tmp_path, deck_path):
    planner = ScriptedPlanner(["good"]); result = polish(..., classifier=HeuristicClassifier())
    assert planner.labels[0][1].type == "title" and result.qa_report["labels"]["1"]["type"] == "title"
    assert result.attempts[0].stage == "classify"

def test_classifier_outage_does_not_fail_the_job(tmp_path, deck_path):
    class Down:  model = "claude-haiku-5-5"; name = "claude"
        def classify(self, deck): return ClassifyAttempt(None, self.model, Usage(0, 0), 0.0, 1, error="rate limited", retryable=True)
    result = polish(..., classifier=Down()); assert result.ok and result.labels is None
```

- [ ] **Step 2: Run to verify they fail**, **Step 3: Implement**, **Step 4: Run `tests/ -q`**

- [ ] **Step 5: Commit**

```bash
git add tests/test_classify.py
git commit -am "Classify slides with a cheap model before planning"
```

---

### Task 10: Vision QA behind a flag

**Files:**
- Create: `polisher/vision.py`
- Modify: `polisher/pipeline.py`, `polisher/cli.py`
- Test: `tests/test_vision.py` (new), `tests/test_pipeline.py`

**Interfaces:**
```python
VISION_THRESHOLD = 4.0
VISION_PROMPT: str      # the document's vision QA prompt; scores alignment, balance, readability, consistency 1 to 5; faults as {shape_hint, problem}
VISION_SCHEMA: dict     # {"alignment": int, "balance": int, "readability": int, "consistency": int, "faults": [{"shape_hint", "problem"}]}
@dataclass
class SlideReview: index: int; score: float; faults: list[str]     # score = mean of the four; faults "shape_hint: problem"
@dataclass
class VisionAttempt: reviews: dict | None; model: str | None; usage: Usage; cost_usd: float; duration_ms: int
    error: str | None = None; retryable: bool = False
class VisionQA:
    name = "claude"
    def __init__(self, provider, model="claude-haiku-5-5", effort="low", pricing=None)
    def review(self, pngs, indexes) -> VisionAttempt
        # one complete_json call per index, images=[previous slide png (if any), this slide png]; usage summed;
        # a ProviderError stops the loop and returns the reviews so far with error set
```
- Pipeline: when `vision` is given, after the deterministic loop: `pngs = (preview_fn or render_previews)(output, preview_dir)` (`preview_dir` required, `ValueError` otherwise), `result.previews = pngs`, timings `previews`; review only slides with no faults (per the document); each review with `score < VISION_THRESHOLD` adds `Fault("vision", "; ".join(faults), repairable=True)` to that slide and sets `vision_score`; one more repair pass through the same `_repair_pass` helper (counts toward `repair_passes`, does not exceed the max); if a repair was accepted, previews are rendered again. Record `stage="vision"`.
- `qa_report["vision_qa"]` is true when vision ran.
- CLI: `--vision-qa` flag (needs `--preview DIR`, which becomes the pipeline's `preview_dir`).

- [ ] **Step 1: Write the failing tests**

```python
def test_vision_qa_scores_each_slide_and_lists_faults(tmp_path):
    provider = FakeProvider([{"alignment": 5, "balance": 5, "readability": 5, "consistency": 5, "faults": []},
                             {"alignment": 2, "balance": 3, "readability": 3, "consistency": 4, "faults": [{"shape_hint": "body", "problem": "cramped"}]}])
    attempt = VisionQA(provider).review([png1, png2], [1, 2])
    assert attempt.reviews[1].score == 5 and attempt.reviews[2].score == 3 and attempt.reviews[2].faults == ["body: cramped"]
    assert len(provider.calls[1]["images"]) == 2  # previous slide travels with the current one

def test_vision_stops_on_provider_error_and_keeps_partial_reviews():

def test_vision_schema_forbids_additional_properties():
```

`tests/test_pipeline.py`:
```python
def test_vision_faults_trigger_a_repair_and_previews_are_returned(tmp_path, deck_path):
    # fake preview_fn writes one PNG per slide; vision scores slide 2 at 2.5; planner.repair returns a fallback
    assert result.ok and result.qa_report["vision_qa"] and result.previews and result.repair_passes == 1
    assert [a.stage for a in result.attempts] == ["plan", "vision", "repair"]

def test_vision_outage_does_not_fail_the_job(tmp_path, deck_path):
```

- [ ] **Step 2: Run to verify they fail**, **Step 3: Implement**, **Step 4: Run `tests/ -q`**

- [ ] **Step 5: Commit**

```bash
git add polisher/vision.py tests/test_vision.py
git commit -am "Score rendered slides with a vision model behind a flag"
```

---

### Task 11: QA report, stages and limits in the web app

**Files:**
- Modify: `orders/models.py`, `orders/processing.py`, `orders/tasks.py`, `orders/views.py`, `orders/admin.py`, `subra/settings.py`, `templates/orders/detail.html`, `.env.example`
- Create: `orders/migrations/0007_qa_report_and_stage.py` (via `makemigrations`)
- Test: `orders/tests/test_processing.py`, `orders/tests/test_views.py`, `orders/tests/test_admin.py`

**Interfaces:**
- `Order.qa_report = models.JSONField(default=dict, blank=True)`; `Order.qa_notes -> list[str]` (the two customer sentences from Global Constraints, built from `qa_report["repaired_slides"]` and `qa_report["fallback_slides"] + flagged`).
- `AIJob.stage = models.CharField(max_length=12, default="plan")`; `log_attempt` writes `record.stage`.
- Settings: `CLASSIFIER_MODEL` (default `claude-haiku-5-5`), `POLISHER_CLASSIFIER` (bool, default True), `VISION_QA` (bool, default False), `VISION_MODEL` (default `claude-haiku-5-5`), `JOB_TIME_LIMIT_SECONDS` (default 600).
- `processing.build_reviewers() -> (classifier | None, vision | None)`: heuristic planner gets `HeuristicClassifier()` and no vision; Claude planner gets `ClaudeClassifier` when `POLISHER_CLASSIFIER`, `VisionQA` when `VISION_QA`.
- `run_polish` passes them plus `preview_dir=tmp / "previews"` and uses `result.previews or make_previews(...)`; the READY transition sets `qa_report=result.qa_report`, `overflow_slides=result.overflow_slides`, and its note gains the timings, e.g. `Verified, 2 attempt(s), $0.0310, 41s (plan 29s, repair 6s, previews 5s)`. `run_rerender` sets `overflow_slides` from `result.overflow_slides` and `qa_report["flagged_slides"]` likewise.
- `tasks.process_order` and `tasks.rerender_order` get `soft_time_limit=settings.JOB_TIME_LIMIT_SECONDS, time_limit=settings.JOB_TIME_LIMIT_SECONDS + 60`.
- View `detail`: `context["qa_notes"] = order.qa_notes` replaces `context["overflow"]`; template shows each note in the existing `bg-marigold-soft` paragraph style and drops the overflow paragraph.
- Admin: `qa_report` in `OrderAdmin.readonly_fields`; `stage` in `AIJobInline.fields` and `AIJobAdmin.list_display`/`list_filter`.

- [ ] **Step 1: Write the failing tests**

```python
def test_qa_report_and_stage_are_saved(queued_order, fake_previews):
    tasks.process_order(queued_order.pk); order.refresh_from_db()
    assert order.qa_report["retention"]["passed"] is True and order.qa_report["flagged_slides"] == []
    assert {j.stage for j in order.ai_jobs.all()} == {"classify", "plan"}

def test_qa_notes_name_repaired_and_flagged_slides():
    order.qa_report = {"repaired_slides": [4, 9], "fallback_slides": [7], "flagged_slides": [7]}
    assert order.qa_notes == ["We re-laid out slides 4 and 9 so everything fits.",
                              "Slide 7 has more content than fits comfortably on one slide, so we used a simpler layout. Check it in PowerPoint."]

def test_detail_shows_qa_notes(client, ready_order):  # set qa_report then GET → the sentence is in the page
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest orders/tests -q`
Expected: FAIL (no `qa_report` field).

- [ ] **Step 3: Implement, then make the migration**

Run: `.venv/bin/python manage.py makemigrations orders --settings=subra.test_settings -n qa_report_and_stage`

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add orders/migrations/0007_qa_report_and_stage.py
git commit -am "Show the QA report on the order and log AI cost per stage"
```

---

### Task 12: Layout sampler and the render-every-layout page

**Files:**
- Create: `polisher/samples.py`, `scripts/render_layouts.py`
- Test: `tests/test_samples.py` (new)

**Interfaces:**
- `samples.build_sampler_deck(path) -> Path`: a 12-slide 16:9 deck with content for every layout, in library order: 1 title (title + subtitle), 2 section (title + one line), 3 agenda (title + 5 items), 4 bullets (title + 6 points with one sub-level), 5 two_column (title + two text boxes with a heading each), 6 stats (title + 4 figures), 7 table (title + 6x4 table), 8 chart (title + line chart + 2-paragraph takeaway), 9 image_text (title + picture + 3 bullets), 10 image_full (title + one large picture), 11 timeline (title + 5 dated steps), 12 quote (one statement + attribution).
- `samples.sampler_plan(deck) -> dict`: the plan dict that gives slide n the n-th layout, built from the extracted ids; `validate_plan(parse_plan(sampler_plan(deck)), deck) == []`.
- `scripts/render_layouts.py`: `main(argv)` with `--out DIR` (required), `--themes` (comma list, default all), `--no-png`. Renders `DIR/<theme>.pptx`, runs `check_deck`, writes `DIR/<theme>/slide-NN.png` with `render_previews` unless `--no-png`, writes `DIR/index.html` (a table: one row per layout, one column per theme, each cell the PNG) and prints every fault. Exit code 1 if any fault.

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("theme", sorted(THEMES))
def test_sampler_renders_every_layout_without_faults(tmp_path, theme):
    deck = extract_deck(build_sampler_deck(tmp_path / "s.pptx"))
    plan = parse_plan(sampler_plan(deck)); assert validate_plan(plan, deck) == []
    assert [p.layout for p in plan.slides] == list(LAYOUTS)
    report = render(plan, deck, get_theme(theme), out, src)
    assert verify(deck, extract_deck(out)).passed
    assert check_deck(out, report, get_theme(theme)).flagged_slides == []

def test_render_layouts_script_writes_decks_and_an_index(tmp_path):
    assert main(["--out", str(tmp_path), "--no-png", "--themes", "navy"]) == 0
    assert (tmp_path / "navy.pptx").exists() and (tmp_path / "index.html").exists()
```

- [ ] **Step 2: Run to verify they fail**, **Step 3: Implement**, **Step 4: Run `tests/ -q`**

- [ ] **Step 5: Run the page for real and look at it**

Run: `.venv/bin/python scripts/render_layouts.py --out /tmp/claude-1000/-home-joe-Subra/8ecb07f6-41fe-4c81-8443-9bb4286892aa/scratchpad/layouts`
Expected: exit 0, 36 PNGs. Read a few PNGs (agenda, timeline, image_full in each theme) and fix anything that looks wrong before committing.

- [ ] **Step 6: Commit**

```bash
git add polisher/samples.py scripts/render_layouts.py tests/test_samples.py
git commit -am "Render every layout in every theme as a sample page"
```

---

### Task 13: Eval set runner

**Files:**
- Create: `scripts/eval_decks.py`
- Test: `tests/test_eval_script.py` (new)

**Interfaces:**
- `run(deck_dir, out_path, planner="heuristic", theme="navy", baseline=None) -> dict`: polishes every `*.pptx` in `deck_dir` (sorted) into a temp dir with `polish(...)` (Claude planner and classifier when `planner == "claude"`), collecting per deck `{"name", "ok", "slides", "retention_passed", "cost_usd", "total_ms", "repaired", "fallbacks", "flagged", "fault_kinds", "human_score"}` (human scores from `deck_dir/scores.json`, `{"<name>.pptx": 1..5}`, when present) and a `summary` with `decks`, `passed`, `retention_rate`, `avg_cost_usd`, `avg_total_ms`, `fallback_rate`, `flagged_rate`, `avg_human_score`; writes the JSON to `out_path`. With `baseline` (a previous JSON), adds `"regressions"`: a list of sentences for retention falling, average cost or time up more than 20%, or fallback or flagged rate rising.
- `main(argv)`: `deck_dir`, `--out FILE` (default `eval/<UTC timestamp>.json`), `--planner`, `--theme`, `--baseline FILE`; prints one line per deck and the summary; exit 1 on any regression or failed deck.

- [ ] **Step 1: Write the failing test**

```python
def test_eval_runs_a_folder_and_writes_a_summary(tmp_path):
    build_rich_deck(tmp_path / "decks" / "a.pptx"); build_business_deck(tmp_path / "decks" / "b.pptx")
    (tmp_path / "decks" / "scores.json").write_text('{"a.pptx": 4}')
    data = run(tmp_path / "decks", tmp_path / "out.json")
    assert data["summary"]["decks"] == 2 and data["summary"]["retention_rate"] == 1.0 and data["summary"]["avg_human_score"] == 4
    again = run(tmp_path / "decks", tmp_path / "out2.json", baseline=tmp_path / "out.json")
    assert again["regressions"] == []
```

- [ ] **Step 2: Run to verify it fails**, **Step 3: Implement**, **Step 4: Run `tests/test_eval_script.py -q`**

- [ ] **Step 5: Commit**

```bash
git add scripts/eval_decks.py tests/test_eval_script.py
git commit -am "Add an eval runner that tracks retention, cost, time and fallbacks per deck"
```

---

### Task 14: Documentation and final verification

**Files:**
- Modify: `README.md`, `PROGRESS.md`, `.env.example`

- [ ] **Step 1: README**: pipeline sentence gains classify, QA and repair; CLI section lists `--classifier`, `--vision-qa`, `--repair-passes`; new "Layout sample page" and "Eval set" sections with the two script commands; env vars.
- [ ] **Step 2: PROGRESS.md**: a "17 Oct build plan (added 22 Oct 2026)" block under Done with what was built, the layout mapping, and what was deliberately not built (planner batching, content-hash cache, IntaSend, 3-slide preview) with one line of why each; update Known issues (vision QA off by default and untested against the API; tables may floor at 10pt).
- [ ] **Step 3: Run everything**

Run: `.venv/bin/python -m pytest -q -p no:cacheprovider` and `.venv/bin/python -m polisher "test-decks/Business update.pptx" --planner heuristic --theme savanna -o /tmp/claude-1000/-home-joe-Subra/8ecb07f6-41fe-4c81-8443-9bb4286892aa/scratchpad/bu.pptx --report /tmp/claude-1000/-home-joe-Subra/8ecb07f6-41fe-4c81-8443-9bb4286892aa/scratchpad/bu.json`
Expected: all tests pass; the CLI prints `verified` and a `QA:` line.

- [ ] **Step 4: Commit**

```bash
git commit -am "Document the quality gates, new layouts and eval tooling"
```
