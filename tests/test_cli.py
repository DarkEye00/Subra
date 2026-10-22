import json

from deckbuilder import build_rich_deck
from polisher.cli import main


def test_cli_polishes_with_heuristic_planner(tmp_path, capsys):
    src = build_rich_deck(tmp_path / "in.pptx")
    out = tmp_path / "out.pptx"
    report = tmp_path / "report.json"
    code = main([str(src), "--theme", "slate", "-o", str(out), "--planner", "heuristic", "--report", str(report)])
    assert code == 0
    assert out.exists()
    data = json.loads(report.read_text())
    assert data["ok"] is True and data["attempts"][0]["planner"] == "heuristic"
    assert "verified" in capsys.readouterr().out.lower()


def test_cli_extract_only_prints_content_json(tmp_path, capsys):
    src = build_rich_deck(tmp_path / "in.pptx")
    assert main([str(src), "--extract-only"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["slides"][0]["texts"][0]["text"] == "Quarterly Business Review"


def test_cli_reports_validation_errors(tmp_path, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF")
    assert main([str(bad), "-o", str(tmp_path / "o.pptx"), "--planner", "heuristic"]) == 2
    assert ".pptx" in capsys.readouterr().err


def test_cli_report_includes_qa_and_timings(tmp_path, capsys):
    src = build_rich_deck(tmp_path / "in.pptx")
    report = tmp_path / "report.json"
    code = main([str(src), "-o", str(tmp_path / "out.pptx"), "--planner", "heuristic", "--report", str(report),
                 "--repair-passes", "1"])
    assert code == 0
    data = json.loads(report.read_text())
    assert data["qa"]["retention"]["passed"] is True and "total" in data["timings_ms"]
    out = capsys.readouterr().out
    assert "QA:" in out and "timings:" in out
