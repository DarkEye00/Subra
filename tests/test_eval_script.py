"""The eval runner: polish a folder of decks and track retention, cost, time and fallbacks (17 Oct build plan)."""

import json

from deckbuilder import build_business_deck, build_rich_deck
from scripts.eval_decks import main, run


def test_eval_runs_a_folder_and_writes_a_summary(tmp_path):
    decks = tmp_path / "decks"
    decks.mkdir()
    build_rich_deck(decks / "a.pptx")
    build_business_deck(decks / "b.pptx")
    (decks / "scores.json").write_text('{"a.pptx": 4}')
    data = run(decks, tmp_path / "out.json")
    summary = data["summary"]
    assert summary["decks"] == 2 and summary["passed"] == 2 and summary["retention_rate"] == 1.0
    assert summary["avg_human_score"] == 4 and summary["fallback_rate"] == 0 and summary["flagged_rate"] == 0
    assert summary["avg_cost_usd"] == 0 and summary["avg_total_ms"] > 0
    assert [d["name"] for d in data["decks"]] == ["a.pptx", "b.pptx"]
    assert data["decks"][0]["human_score"] == 4 and data["decks"][1]["human_score"] is None
    assert data["decks"][0]["slides"] == 7 and data["decks"][0]["retention_passed"] is True
    assert json.loads((tmp_path / "out.json").read_text())["summary"]["decks"] == 2
    again = run(decks, tmp_path / "out2.json", baseline=tmp_path / "out.json")
    assert again["regressions"] == []


def test_eval_reports_regressions_against_a_baseline(tmp_path):
    decks = tmp_path / "decks"
    decks.mkdir()
    build_rich_deck(decks / "a.pptx")
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({"summary": {"decks": 1, "passed": 1, "retention_rate": 1.0, "avg_cost_usd": 0,
                                                "avg_total_ms": 1, "fallback_rate": 0, "flagged_rate": 0,
                                                "avg_human_score": None}}))
    data = run(decks, tmp_path / "out.json", baseline=baseline)
    assert any("time" in r for r in data["regressions"])  # anything is slower than 1ms
    assert main([str(decks), "--out", str(tmp_path / "out3.json"), "--baseline", str(baseline)]) == 1
    assert main([str(decks), "--out", str(tmp_path / "out4.json")]) == 0
