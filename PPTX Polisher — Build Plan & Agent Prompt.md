# PPTX Polisher — Build Plan & Agent Prompt

Oct 17, 2026 · @Joe

## Goal and quality bar

The polisher takes any corporate .pptx and returns a redesigned, fully editable deck with every data point intact. That retention guarantee is the product: Oreate and Gamma generate from a prompt, so they can drop or invent content; a polisher cannot.

A deck passes when all of the following hold:

- Every text run, number, table cell and chart series from the source appears in the output (checked by script, not by eye).
- No text overflows its box, no element overlaps another, no font smaller than 12 pt on body text.
- Charts and tables are native PowerPoint objects, not images.
- Speaker notes, slide order and section structure are preserved.
- A 20-slide deck finishes in under 3 minutes and costs under $0.40 in model calls.

Target customers are Kenyan corporates and consultancies. Pricing is per deck at launch, so cost per deck and failure rate decide the margin.

## Pipeline architecture

The model never positions a shape. Code parses the deck, fills fixed layouts, renders the result and verifies it; the model decides which layout fits each slide and judges the render.

&#91;embedded content: polisher pipeline · 9 stages, 1 repair loop\]

The repair loop (steps 5 to 7) runs at most twice; a slide still failing after that falls back to the plainest safe layout and is flagged in the QA report.

Stage notes:

1. **Parse.** Extract per slide: title, body runs with formatting, tables as rows, charts as series plus chart type, images with alt text, notes. Keep source ids on everything so step 8 can diff.
2. **Classify.** A cheap model labels each slide (title, agenda, bullets, comparison, KPI, table, chart, image-led, quote, closing) and its intent in one sentence. Batch all slides in one call.
3. **Plan.** A strong model receives the slide JSON, the layout library spec and the theme, and returns a layout id plus a content mapping per slide. It may split one dense slide into two, never merge or drop.
4. **Build.** python-pptx (or a JS equivalent) fills the chosen layout. Tables and charts are rebuilt as native objects; images are placed in their slot.
5. **Render.** LibreOffice headless converts to PDF, then to PNG per slide.
6. **Check.** Deterministic rules first: overflow via text-metrics estimate, overlap via bounding boxes, min font size, contrast. Then a vision model scores each PNG and lists faults.
7. **Repair.** Only flagged slides go back to Plan with the fault list attached.
8. **Verify data.** Normalise both decks to plain text and numbers and diff; any missing item fails the job.
9. **Deliver.** The .pptx, the slide thumbnails and a short QA report (retention 100%, slides repaired, fallbacks used).

## Layout and theme system

Clean output comes from a small, hand-built layout library with hard capacity limits, not from the model drawing freely. Start with the 16 layouts below; each is a function that takes a theme and a content object and returns a slide.

| Layout id | Use for | Capacity limits |
| --- | --- | --- |
| title | Cover | title 60 chars, subtitle 90 |
| section | Divider | title 40 chars |
| agenda | Contents | 3 to 7 items, 50 chars each |
| bullets-1col | Plain points | 3 to 6 bullets, 90 chars, 1 sub-level |
| bullets-2col | Many points | 2 columns of 3 to 5 bullets, 60 chars |
| statement | One big message | 120 chars |
| kpi-3 / kpi-4 | Metrics | 3 or 4 tiles: value 12 chars, label 30 |
| comparison | A vs B | 2 columns, header + 4 rows of 50 chars |
| table | Data table | up to 8 rows x 6 cols; larger splits across slides |
| chart-full | One chart | native chart, caption 90 chars |
| chart-text | Chart + takeaway | chart left, 3 bullets right |
| image-text | Photo + points | image left, title + 4 bullets right |
| image-full | Hero image | title overlay 60 chars |
| timeline | Steps or dates | 3 to 6 nodes, label 30 chars |
| quote | Testimonial | 180 chars + attribution |
| closing | Thank you / contact | title + 3 contact lines |

Rules that keep it clean:

- Capacity limits are enforced in code. The planner must split a slide rather than exceed a limit; the builder refuses content that does.
- A theme is a JSON file: primary and accent colours, two fonts (heading, body), logo path, background style, footer text. Ship 3 to 4 house themes and allow one per client later.
- Spacing lives in the theme too: margins 0.5 in, gutter 0.3 in, a 12-column grid. Every layout reads these values so one change restyles everything.
- Body text 14 to 18 pt, titles 28 to 36 pt, never below 12 pt anywhere.
- Charts use the theme palette in order; tables use header fill plus zebra rows, no vertical borders.
- Icons come from a bundled open set mapped by keyword; never generated per deck.

Build the layouts first as a standalone package with a visual test page that renders every layout with sample content. If a layout looks wrong with sample content it will look wrong in production.

## Quality gates

Run the cheap deterministic checks first and only send slides that pass them to the vision model; this cuts model cost and catches most faults.

**Deterministic checks (code, per slide)**

- Overflow: estimate text height from character count, font size and box width; fail if it exceeds the box by more than 5%.
- Overlap: compare bounding boxes of all shapes; fail on any intersection except image-over-background.
- Min font size 12 pt; min contrast 4.5:1 between text and its fill.
- Empty placeholders: fail if a layout slot is unfilled.
- Chart sanity: series count matches source, axis labels present.

**Vision QA (model, per rendered PNG)**

Score each slide 1 to 5 on alignment, balance, readability and consistency with the previous slide. Return a JSON list of faults with shape ids. Anything under 4 goes to repair.

**Data retention check (code, whole deck)**

Normalise source and output to lowercase text tokens and numbers, ignoring whitespace and punctuation. Every source token must appear in the output. Report the retention percentage; the job fails below 100% unless the user opted into summarisation.

**Eval set**

Collect 20 to 30 real decks (ask friends in corporates, strip confidential data). For each, store the source, the output and a 1 to 5 human score. Re-run the whole set after every meaningful change and track average score, retention, cost and time per deck. Do not ship a change that lowers any of those.

## Agent prompts

Three prompts run inside the pipeline (classifier, planner, vision QA) and one builds the system (the Claude Code prompt). Keep the runtime prompts short and force JSON output; put every rule into the schema, not into prose the model may skim.

**Classifier (cheap model, one call per deck)**

```markdown
You classify PowerPoint slides. For each slide in the JSON array, return
{"slide_id", "type", "intent"} where type is one of: title, section, agenda,
bullets, statement, kpi, comparison, table, chart, image-led, timeline, quote,
closing, mixed. intent is one sentence on what the slide is trying to say.
Return only a JSON array. Never drop a slide.
```

**Planner (strong model, one call per batch of 5 to 8 slides)**

```markdown
You are a presentation designer choosing layouts for a corporate deck.

You receive: the theme, the layout library with capacity limits, and a batch
of slides as JSON (content, type, intent, and for repairs a fault list).

For each slide return {"slide_id", "layout_id", "mapping", "split": [...]}.
- mapping assigns every content item from the source to a slot in the layout.
- Every source item must appear in exactly one slot. Do not summarise,
  shorten, paraphrase or drop anything. Do not add text.
- If content exceeds a layout's limits, split it into two or more slides
  using the same or a related layout; put them in "split" in order.
- Prefer the layout that matches the slide's type; use statement or kpi
  when a bullets slide holds 1 to 3 short, punchy items.
- Keep consecutive slides visually varied: avoid the same layout three
  times in a row unless the content demands it.
- For a repair, the fault list tells you what failed. Choose a roomier
  layout or split; do not simply shrink text.
Return only JSON matching the schema.
```

**Vision QA (vision model, one call per rendered slide)**

```markdown
You review a rendered slide image for design quality. Score 1 to 5 on
alignment, balance, readability, and consistency with the previous slide
(also provided). List faults as {"shape_hint", "problem"} using plain
words: overflow, overlap, cramped, empty space, tiny text, misaligned,
clashing colour. Do not comment on wording or content. Return only JSON.
```

**Claude Code build prompt (paste at the start of the session)**

```markdown
You are building a PowerPoint polisher service. Input: a .pptx. Output: a
redesigned, fully editable .pptx with 100% of the source content retained.

Architecture (follow it exactly):
1. parse/ - python-pptx extracts each slide into a Pydantic model: title,
   text runs with formatting, tables as rows, charts as series + chart
   type, images with alt text, notes. Every item keeps a stable source_id.
2. layouts/ - a library of 16 layout functions (title, section, agenda,
   bullets-1col, bullets-2col, statement, kpi-3, kpi-4, comparison, table,
   chart-full, chart-text, image-text, image-full, timeline, quote, closing).
   Each takes (theme, content) and returns a slide. Each declares capacity
   limits and raises CapacityError when exceeded. Themes are JSON files.
   Include a script that renders every layout with sample content to PNG.
3. agents/ - classifier, planner and vision QA as thin functions that call
   the model API with the prompts in prompts/ and validate output against
   JSON schemas. Retry once on invalid JSON, then fail the slide.
4. build/ - fills layouts from the planner output. Tables and charts are
   native objects. Images are placed, never regenerated.
5. qa/ - deterministic checks (overflow estimate, bbox overlap, min font
   12pt, contrast 4.5:1, empty slots, chart series count), then LibreOffice
   headless render to PNG, then vision QA. Repair loop max 2 passes;
   fallback to bullets-1col and flag the slide.
6. verify/ - normalise source and output to tokens and numbers; diff;
   fail the job below 100% retention.
7. api/ - a job endpoint: upload, status, download, QA report JSON.
   Use Celery with Redis for the queue; one job per worker; render in a
   subprocess with a timeout.

Rules:
- Model calls are batched: one classifier call per deck, planner calls in
  batches of 5 to 8 slides, vision QA only on slides that passed the
  deterministic checks.
- Log cost (tokens) and time per stage per job to a table.
- Write tests: unit tests for every layout's capacity limits, parse/build
  round-trip tests on three sample decks, and a retention test that must
  pass at 100%.
- Build in this order and stop for review after each: layouts with the
  sample render page, parse + verify round-trip, planner + build on one
  deck, QA loop, API and queue.

Start with layouts/. Show me the rendered sample page before moving on.
```

## Scaling and cost

The pipeline scales because most of it is code, and the model calls are batched and routed by task. Per-deck cost is the number to watch from day one.

| Stage | Model tier | Calls per 20-slide deck | Why |
| --- | --- | --- | --- |
| Classify | cheap (Haiku-class) | 1 | labels only |
| Plan | strong (Sonnet-class) | 3 to 4 | layout judgement |
| Vision QA | vision, mid tier | 10 to 20 | one image each, only after code checks pass |
| Repair plan | strong | 0 to 2 | flagged slides only |

Infrastructure that holds up under load:

- Queue every job (Celery + Redis). Workers are stateless; scale them horizontally. Each job gets a temp directory and a hard timeout of 5 minutes.
- Run LibreOffice in its own container image with a warm process pool; cold starts are the slowest part of the loop.
- Store uploads and outputs in object storage (S3-compatible), not on the worker disk; keep outputs 7 days, then delete.
- Cache theme renders and layout thumbnails; cache planner output keyed by slide content hash so re-runs of an edited deck only re-plan changed slides.
- Record tokens, latency and QA outcome per stage per job. Alert when average cost per deck or fallback rate rises.
- Rate-limit per account and queue by priority so one large deck cannot block paying users.

For the Kenyan market: keep the server in a region with low latency to Nairobi, support M-Pesa via IntaSend at launch, and show a preview of 3 slides before charging so users trust the output.

## Build order

Rebuild around the layout library first and migrate the MVP's working parts into the new structure; do not keep patching free-form generation.

1. **Layouts and themes (week 1).** 16 layouts, 3 themes, sample render page. Review every layout by eye before writing any model code.
2. **Parse and verify (week 1).** Round-trip three real decks through parse, build and verify at 100% retention with a dumb planner that picks bullets-1col for everything.
3. **Planner (week 2).** Add the real planner; run on the eval set; compare human scores against the dumb planner.
4. **QA loop (week 2).** Deterministic checks, render, vision QA, repair. Measure how many slides the loop fixes and what it costs.
5. **API, queue and payments (week 3).** Job endpoint, Celery workers, preview-then-pay flow, QA report in the UI.
6. **Eval and tune (ongoing).** Grow the eval set to 30 decks; re-run weekly; tune prompts and capacity limits from the failures.

When you share the MVP, the first thing to look at is which of these stages it already does well and which it skips; most MVPs skip stages 1, 4 and 6.
