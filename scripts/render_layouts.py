#!/usr/bin/env python
"""Render every layout with sample content in every theme, and build a contact sheet to review by eye.

    .venv/bin/python scripts/render_layouts.py --out out/layouts
    open out/layouts/index.html

Writes <out>/<theme>.pptx, <out>/<theme>/slide-NN.png (needs LibreOffice unless --no-png) and <out>/index.html.
Exit code 1 if any slide fails the deterministic QA checks (17 Oct build plan, "review every layout by eye").
"""

import argparse
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polisher.extract import extract_deck  # noqa: E402
from polisher.layouts import LAYOUTS  # noqa: E402
from polisher.plan import parse_plan  # noqa: E402
from polisher.qa import check_deck  # noqa: E402
from polisher.render.renderer import render  # noqa: E402
from polisher.render.themes import THEMES, get_theme  # noqa: E402
from polisher.samples import build_sampler_deck, sampler_plan  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render every layout in every theme as a sample page.")
    ap.add_argument("--out", required=True, metavar="DIR")
    ap.add_argument("--themes", default=",".join(sorted(THEMES)), help="comma-separated theme slugs")
    ap.add_argument("--no-png", action="store_true", help="skip the LibreOffice previews (decks and index only)")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    src = build_sampler_deck(out / "sampler-source.pptx")
    deck = extract_deck(src)
    plan = parse_plan(sampler_plan(deck))
    faults = 0
    pngs = {}
    for slug in themes:
        theme = get_theme(slug)
        pptx = out / f"{slug}.pptx"
        report = render(plan, deck, theme, pptx, src)
        qa = check_deck(pptx, report, theme)
        for s in qa.faulted:
            for f in s.faults:
                faults += 1
                print(f"{slug} slide {s.index} ({s.layout}): {f.kind}: {f.detail}")
        if not args.no_png:
            from polisher.preview import render_previews

            pngs[slug] = render_previews(pptx, out / slug, dpi=60)
        print(f"{slug}: wrote {pptx.name}, {len(qa.flagged_slides)} slide(s) with faults")

    rows = []
    for i, layout in enumerate(LAYOUTS, start=1):
        cells = "".join(
            f'<td><img src="{slug}/{pngs[slug][i - 1].name}" alt="{layout} in {slug}" loading="lazy"></td>'
            if slug in pngs and i <= len(pngs[slug]) else f"<td>{html.escape(slug)}.pptx slide {i}</td>"
            for slug in themes
        )
        rows.append(f"<tr><th>{i}. {layout}</th>{cells}</tr>")
    header = "".join(f"<th>{html.escape(slug)}</th>" for slug in themes)
    (out / "index.html").write_text(
        "<!doctype html><meta charset='utf-8'><title>SUBRA layouts</title>"
        "<style>body{font:14px system-ui;margin:16px}table{border-collapse:collapse}th{text-align:left;"
        "padding:6px}td{padding:6px;vertical-align:top}img{width:360px;border:1px solid #ccc}</style>"
        f"<h1>Every layout in every theme</h1><table><tr><th>Layout</th>{header}</tr>{''.join(rows)}</table>\n"
    )
    print(f"index: {out / 'index.html'}")
    return 1 if faults else 0


if __name__ == "__main__":
    sys.exit(main())
