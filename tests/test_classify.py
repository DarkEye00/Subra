"""The slide classifier (17 Oct build plan, stage 2): cheap labels that are hints for the planner."""

import json

import pytest

from deckbuilder import build_rich_deck
from polisher.ai import ProviderError
from polisher.classify import (CLASSIFY_SCHEMA, SLIDE_TYPES, ClaudeClassifier, HeuristicClassifier, SlideLabel,
                               classifier_input)
from polisher.extract import extract_deck
from test_planner import FakeProvider


@pytest.fixture(scope="module")
def deck(tmp_path_factory):
    return extract_deck(build_rich_deck(tmp_path_factory.mktemp("cl") / "rich.pptx"))


def test_classifier_input_has_every_slide_and_no_binaries(deck):
    data = classifier_input(deck)
    assert [s["slide"] for s in data] == [1, 2, 3, 4, 5, 6, 7]
    assert data[0]["title"] == "Quarterly Business Review" and data[2]["tables"] == 1 and data[3]["charts"] == 1
    assert "blob" not in json.dumps(data) and "chart_xml" not in json.dumps(data)


def test_classifier_input_truncates_long_text(deck):
    deck.slides[1].texts[0].text = "x" * 500
    try:
        entry = classifier_input(deck)[1]
        assert all(len(t) <= 121 for t in entry["text"])
    finally:
        deck.slides[1].texts[0].text = "Revenue grew 45% to KES 12.5m"


def test_claude_classifier_returns_labels(deck):
    provider = FakeProvider([{"slides": [{"slide": 1, "type": "title", "intent": "Opens the review"},
                                         {"slide": 2, "type": "kpi", "intent": "Headline numbers"}]}])
    attempt = ClaudeClassifier(provider).classify(deck)
    assert attempt.error is None and attempt.labels[1] == SlideLabel("title", "Opens the review")
    assert attempt.labels[2].type == "kpi" and 3 not in attempt.labels
    call = provider.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["effort"] == "low" and call["schema"] == CLASSIFY_SCHEMA
    assert "Never drop a slide" in call["system"] and "Quarterly Business Review" in call["user"]
    assert attempt.cost_usd > 0 and attempt.model == "claude-haiku-5-5"


def test_classifier_error_returns_no_labels(deck):
    attempt = ClaudeClassifier(FakeProvider([ProviderError("down", retryable=True)])).classify(deck)
    assert attempt.labels is None and attempt.retryable and "down" in attempt.error


def test_classifier_ignores_unknown_slides_and_types(deck):
    provider = FakeProvider([{"slides": [{"slide": 99, "type": "title", "intent": "?"},
                                         {"slide": 1, "type": "poster", "intent": "?"}]}])
    attempt = ClaudeClassifier(provider).classify(deck)
    assert attempt.labels == {} and attempt.error is None


def test_heuristic_classifier_labels_the_rich_deck(deck):
    labels = HeuristicClassifier().classify(deck).labels
    assert labels[1].type == "title" and labels[3].type == "table" and labels[4].type == "chart"
    assert labels[5].type == "image-led" and labels[7].type == "closing" and labels[2].type == "bullets"
    assert set(l.type for l in labels.values()) <= set(SLIDE_TYPES)


def test_schema_forbids_additional_properties():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(CLASSIFY_SCHEMA)
    assert CLASSIFY_SCHEMA["properties"]["slides"]["items"]["properties"]["type"]["enum"] == list(SLIDE_TYPES)
