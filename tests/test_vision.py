"""Vision QA (17 Oct build plan, stage 6b): a vision model scores rendered slides, behind a flag."""

import pytest

from polisher.ai import ProviderError
from polisher.vision import VISION_SCHEMA, VISION_THRESHOLD, VisionQA
from test_planner import FakeProvider


def _pngs(tmp_path, n):
    out = []
    for i in range(1, n + 1):
        p = tmp_path / f"slide-{i}.png"
        p.write_bytes(b"\x89PNG" + bytes([i]))
        out.append(p)
    return out


def _scores(a, b, r, c, faults=()):
    return {"alignment": a, "balance": b, "readability": r, "consistency": c,
            "faults": [{"shape_hint": h, "problem": p} for h, p in faults]}


def test_vision_qa_scores_each_slide_and_lists_faults(tmp_path):
    provider = FakeProvider([_scores(5, 5, 5, 5), _scores(2, 3, 3, 4, [("body", "cramped")])])
    attempt = VisionQA(provider).review(_pngs(tmp_path, 2), [1, 2])
    assert attempt.error is None and attempt.model == "claude-haiku-5-5"
    assert attempt.reviews[1].score == 5 and attempt.reviews[1].faults == []
    assert attempt.reviews[2].score == 3 and attempt.reviews[2].faults == ["body: cramped"]
    assert len(provider.calls[0]["images"]) == 1  # the first slide travels alone
    assert len(provider.calls[1]["images"]) == 2  # the previous slide travels with the current one
    assert provider.calls[1]["images"][1] == b"\x89PNG\x02"
    assert provider.calls[0]["schema"] == VISION_SCHEMA and provider.calls[0]["effort"] == "low"
    assert attempt.usage.input_tokens == 2000 and attempt.cost_usd > 0


def test_vision_reviews_only_the_slides_asked_for(tmp_path):
    provider = FakeProvider([_scores(4, 4, 4, 4)])
    attempt = VisionQA(provider).review(_pngs(tmp_path, 3), [3])
    assert list(attempt.reviews) == [3] and len(provider.calls) == 1
    assert len(provider.calls[0]["images"]) == 2


def test_vision_stops_on_provider_error_and_keeps_partial_reviews(tmp_path):
    provider = FakeProvider([_scores(5, 5, 5, 5), ProviderError("rate limited", retryable=True)])
    attempt = VisionQA(provider).review(_pngs(tmp_path, 3), [1, 2, 3])
    assert list(attempt.reviews) == [1] and attempt.retryable and "rate limited" in attempt.error
    assert len(provider.calls) == 2


def test_vision_threshold_is_four():
    assert VISION_THRESHOLD == 4.0


def test_vision_schema_forbids_additional_properties():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(VISION_SCHEMA)
    assert set(VISION_SCHEMA["required"]) == {"alignment", "balance", "readability", "consistency", "faults"}
