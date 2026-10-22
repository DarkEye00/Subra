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


def _agents(kind, classify="on", vision_qa=False):
    """(planner, escalation, classifier, vision) for the CLI. Heuristic runs make no API calls unless vision
    QA is asked for, which always needs the API."""
    from .classify import ClaudeClassifier, HeuristicClassifier
    from .planner import ClaudePlanner, HeuristicPlanner

    provider = None
    if kind == "claude" or vision_qa:
        from .ai import AnthropicProvider

        provider = AnthropicProvider()
    vision = None
    if vision_qa:
        from .vision import VisionQA

        vision = VisionQA(provider, os.environ.get("VISION_MODEL", "claude-haiku-5-5"))
    if kind == "heuristic":
        return HeuristicPlanner(), None, HeuristicClassifier() if classify == "on" else None, vision
    planner = ClaudePlanner(provider, os.environ.get("PLANNER_MODEL", "claude-sonnet-5"),
                            effort=os.environ.get("PLANNER_EFFORT", "medium"))
    escalation = ClaudePlanner(provider, os.environ.get("ESCALATION_MODEL", "claude-opus-5-5"),
                               effort=os.environ.get("ESCALATION_EFFORT", "high"))
    classifier = None
    if classify == "on":
        classifier = ClaudeClassifier(provider, os.environ.get("CLASSIFIER_MODEL", "claude-haiku-5-5"))
    return planner, escalation, classifier, vision


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m polisher", description="Polish a PowerPoint deck.")
    ap.add_argument("input", help="the .pptx to polish")
    ap.add_argument("-o", "--output", help="where to write the polished .pptx")
    ap.add_argument("--theme", default="navy", choices=sorted(THEMES))
    ap.add_argument("--planner", default="claude", choices=["claude", "heuristic"])
    ap.add_argument("--classifier", default="on", choices=["on", "off"],
                    help="label each slide's type and intent first, as hints for the planner (default on)")
    ap.add_argument("--preview", metavar="DIR", help="also render PNG thumbnails into DIR")
    ap.add_argument("--vision-qa", action="store_true",
                    help="score every rendered slide with a vision model and repair low scores (needs --preview)")
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

    if args.vision_qa and not args.preview:
        ap.error("--vision-qa needs --preview DIR")
    planner, escalation, classifier, vision = _agents(args.planner, args.classifier, args.vision_qa)
    try:
        result = polish(args.input, args.output, theme=args.theme, planner=planner, escalation=escalation,
                        classifier=classifier, vision=vision, preview_dir=args.preview,
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
        if a.error:
            status = a.error
        elif a.stage in ("plan", "repair"):
            status = "verified" if a.verification_passed else "failed verification"
        else:
            status = "done"
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
        pngs = result.previews
        if not pngs:
            from .preview import render_previews

            pngs = render_previews(result.output_path, args.preview)
        print(f"{len(pngs)} previews in {args.preview}")
    return 0
