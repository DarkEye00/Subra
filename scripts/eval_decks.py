#!/usr/bin/env python
"""Polish every deck in a folder and track retention, cost, time, repairs and fallbacks (17 Oct build plan).

    .venv/bin/python scripts/eval_decks.py test-decks                      # heuristic planner, no API calls
    .venv/bin/python scripts/eval_decks.py test-decks --planner claude     # the real pipeline (needs the API key)
    .venv/bin/python scripts/eval_decks.py test-decks --baseline eval/2026-10-22T10-00-00.json

Put human scores (1 to 5) in <folder>/scores.json as {"deck.pptx": 4, ...} and they are averaged into the summary.
Each run writes a JSON file (default eval/<UTC timestamp>.json). With --baseline, the summary is compared with an
earlier run and the exit code is 1 on any regression: retention falling, average cost or time up by more than 20%,
or the fallback or flagged rate rising. Do not ship a change that lowers any of those.
"""

import argparse
import json
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polisher.pipeline import PlannerUnavailable, UnsupportedContentError, polish  # noqa: E402
from polisher.validate import ValidationError  # noqa: E402

TOLERANCE = 0.20  # cost and time may drift this much before it counts as a regression


def _agents(planner):
    from polisher.cli import _agents as cli_agents

    return cli_agents(planner)


def _one(path, planner, escalation, classifier, theme):
    entry = {"name": path.name, "ok": False, "slides": 0, "retention_passed": False, "cost_usd": 0.0,
             "total_ms": 0, "repaired": 0, "fallbacks": 0, "flagged": 0, "fault_kinds": {}, "error": None}
    with tempfile.TemporaryDirectory(prefix="subra-eval-") as tmp:
        started = time.monotonic()
        try:
            result = polish(path, Path(tmp) / "out.pptx", theme=theme, planner=planner, escalation=escalation,
                            classifier=classifier)
        except (ValidationError, UnsupportedContentError, PlannerUnavailable) as e:
            entry["error"] = str(e)
            entry["total_ms"] = int((time.monotonic() - started) * 1000)
            return entry
        report = result.qa_report
        entry.update({
            "ok": result.ok, "slides": result.slide_count, "retention_passed": bool(report["retention"]["passed"]),
            "cost_usd": result.total_cost_usd, "total_ms": result.timings_ms.get("total", 0),
            "repaired": len(report["repaired_slides"]), "fallbacks": len(report["fallback_slides"]),
            "flagged": len(report["flagged_slides"]), "error": result.failure,
        })
        kinds = {}
        for s in report["slides"]:
            for f in s["faults"]:
                kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
        entry["fault_kinds"] = kinds
    return entry


def _summary(decks):
    n = len(decks)
    scored = [d["human_score"] for d in decks if d.get("human_score") is not None]
    return {
        "decks": n,
        "passed": sum(1 for d in decks if d["ok"]),
        "retention_rate": (sum(1 for d in decks if d["retention_passed"]) / n) if n else 0,
        "avg_cost_usd": (sum(d["cost_usd"] for d in decks) / n) if n else 0,
        "avg_total_ms": (sum(d["total_ms"] for d in decks) / n) if n else 0,
        "fallback_rate": (sum(1 for d in decks if d["fallbacks"]) / n) if n else 0,
        "flagged_rate": (sum(1 for d in decks if d["flagged"]) / n) if n else 0,
        "avg_human_score": (sum(scored) / len(scored)) if scored else None,
    }


def _regressions(summary, baseline):
    base = baseline.get("summary", {})
    out = []
    if summary["retention_rate"] < base.get("retention_rate", 0):
        out.append(f"retention: {summary['retention_rate']:.0%} vs {base['retention_rate']:.0%} baseline")
    for key, label in (("avg_cost_usd", "cost"), ("avg_total_ms", "time")):
        before, after = base.get(key) or 0, summary[key]
        if before and after > before * (1 + TOLERANCE):
            out.append(f"{label}: avg {after:.4g} vs {before:.4g} baseline (+{(after / before - 1):.0%})")
    for key, label in (("fallback_rate", "fallback rate"), ("flagged_rate", "flagged rate")):
        if summary[key] > base.get(key, 0):
            out.append(f"{label}: {summary[key]:.0%} vs {base.get(key, 0):.0%} baseline")
    if summary["passed"] < base.get("passed", 0):
        out.append(f"passed: {summary['passed']} vs {base['passed']} baseline")
    return out


def run(deck_dir, out_path, planner="heuristic", theme="navy", baseline=None):
    deck_dir, out_path = Path(deck_dir), Path(out_path)
    planner_obj, escalation, classifier, _ = _agents(planner)
    scores = {}
    if (deck_dir / "scores.json").exists():
        scores = json.loads((deck_dir / "scores.json").read_text())
    decks = []
    for path in sorted(deck_dir.glob("*.pptx")):
        entry = _one(path, planner_obj, escalation, classifier, theme)
        entry["human_score"] = scores.get(path.name)
        decks.append(entry)
        print(f"{entry['name']}: {'ok' if entry['ok'] else 'FAILED'} {entry['slides']} slides "
              f"${entry['cost_usd']:.4f} {entry['total_ms']}ms repaired {entry['repaired']} "
              f"fallbacks {entry['fallbacks']} flagged {entry['flagged']}"
              + (f" ({entry['error']})" if entry["error"] else ""))
    data = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "planner": planner, "theme": theme, "deck_dir": str(deck_dir),
        "decks": decks, "summary": _summary(decks), "regressions": [],
    }
    if baseline is not None:
        data["baseline"] = str(baseline)
        data["regressions"] = _regressions(data["summary"], json.loads(Path(baseline).read_text()))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2))
    s = data["summary"]
    print(f"summary: {s['passed']}/{s['decks']} passed, retention {s['retention_rate']:.0%}, "
          f"avg cost ${s['avg_cost_usd']:.4f}, avg time {s['avg_total_ms'] / 1000:.1f}s, "
          f"fallbacks {s['fallback_rate']:.0%}, flagged {s['flagged_rate']:.0%}, "
          f"human score {s['avg_human_score'] if s['avg_human_score'] is not None else 'n/a'}")
    for r in data["regressions"]:
        print(f"REGRESSION {r}")
    print(f"wrote {out_path}")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser(description="Polish every deck in a folder and summarise the results.")
    ap.add_argument("deck_dir")
    ap.add_argument("--out", metavar="FILE",
                    default=f"eval/{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')}.json")
    ap.add_argument("--planner", default="heuristic", choices=["heuristic", "claude"])
    ap.add_argument("--theme", default="navy")
    ap.add_argument("--baseline", metavar="FILE", help="an earlier run's JSON to compare with")
    args = ap.parse_args(argv)
    data = run(args.deck_dir, args.out, planner=args.planner, theme=args.theme, baseline=args.baseline)
    failed = data["summary"]["passed"] < data["summary"]["decks"]
    return 1 if data["regressions"] or failed else 0


if __name__ == "__main__":
    sys.exit(main())
