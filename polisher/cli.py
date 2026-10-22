"""Command line: python -m polisher input.pptx --theme navy -o output.pptx"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .extract import extract_deck
from .pipeline import PlannerUnavailable, UnsupportedContentError, polish
from .render.themes import THEMES
from .validate import ValidationError


def _planners(kind):
    from .planner import ClaudePlanner, HeuristicPlanner

    if kind == "heuristic":
        return HeuristicPlanner(), None
    from .ai import AnthropicProvider

    provider = AnthropicProvider()
    planner = ClaudePlanner(provider, os.environ.get("PLANNER_MODEL", "claude-sonnet-5"),
                            effort=os.environ.get("PLANNER_EFFORT", "medium"))
    escalation = ClaudePlanner(provider, os.environ.get("ESCALATION_MODEL", "claude-opus-5-5"),
                               effort=os.environ.get("ESCALATION_EFFORT", "high"))
    return planner, escalation


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m polisher", description="Polish a PowerPoint deck.")
    ap.add_argument("input", help="the .pptx to polish")
    ap.add_argument("-o", "--output", help="where to write the polished .pptx")
    ap.add_argument("--theme", default="navy", choices=sorted(THEMES))
    ap.add_argument("--planner", default="claude", choices=["claude", "heuristic"])
    ap.add_argument("--preview", metavar="DIR", help="also render PNG thumbnails into DIR")
    ap.add_argument("--report", metavar="FILE", help="write a JSON report of the run")
    ap.add_argument("--repair-passes", type=int, default=2, metavar="N",
                    help="how many times slides that fail layout checks go back to the planner (default 2)")
    ap.add_argument("--extract-only", action="store_true", help="print the extracted content as JSON and exit")
    args = ap.parse_args(argv)

    if args.extract_only:
        print(json.dumps(extract_deck(args.input).to_dict(), indent=2, ensure_ascii=False))
        return 0
    if not args.output:
        ap.error("-o/--output is required")

    planner, escalation = _planners(args.planner)
    try:
        result = polish(args.input, args.output, theme=args.theme, planner=planner, escalation=escalation,
                        max_repair_passes=args.repair_passes)
    except ValidationError as e:
        print(f"Rejected: {e.message}", file=sys.stderr)
        return 2
    except UnsupportedContentError as e:
        print(f"Rejected: {e}", file=sys.stderr)
        return 2
    except PlannerUnavailable as e:
        print(f"AI planner unavailable, try again later: {e}", file=sys.stderr)
        return 3

    rate = float(os.environ.get("USD_KES_RATE", "129.20"))
    for a in result.attempts:
        status = "verified" if a.verification_passed else (a.error or "failed verification")
        print(f"attempt {a.number} ({a.stage}): {a.planner} {a.model or ''} "
              f"in={a.usage.input_tokens} out={a.usage.output_tokens} "
              f"cache_read={a.usage.cache_read_tokens} ${a.cost_usd:.4f} {a.duration_ms}ms -> {status}")
        for p in a.problems[:10]:
            print(f"    - {p}")
    print(f"total AI cost: ${result.total_cost_usd:.4f} (KES {result.total_cost_usd * rate:.2f})")
    qa = result.qa_report
    print(f"QA: repaired {qa['repaired_slides']}, fallbacks {qa['fallback_slides']}, flagged {qa['flagged_slides']} "
          f"({qa['repair_passes']} repair pass(es))")
    print("timings: " + " ".join(f"{k} {v}ms" for k, v in result.timings_ms.items()))
    for s in qa["slides"]:
        for f in s["faults"]:
            print(f"    slide {s['index']}: {f['kind']}: {f['detail']}")

    if args.report:
        report = {
            "ok": result.ok,
            "slide_count": result.slide_count,
            "total_cost_usd": result.total_cost_usd,
            "overflow_slides": result.overflow_slides,
            "failure": result.failure,
            "attempts": [asdict(a) for a in result.attempts],
            "qa": qa,
            "timings_ms": result.timings_ms,
        }
        Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False))

    if not result.ok:
        print(f"FAILED: {result.failure}", file=sys.stderr)
        return 1
    print(f"verified, wrote {result.output_path}")
    if args.preview:
        from .preview import render_previews

        pngs = render_previews(result.output_path, args.preview)
        print(f"{len(pngs)} previews in {args.preview}")
    return 0
