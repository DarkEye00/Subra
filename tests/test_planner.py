import json
from types import SimpleNamespace

import pytest

from deckbuilder import build_rich_deck
from polisher.ai import AnthropicProvider, JsonResult, ProviderError, Usage
from polisher.cost import cost_usd
from polisher.extract import extract_deck
from polisher.planner import ClaudePlanner, PLAN_SCHEMA, SYSTEM_PROMPT, planner_input
from polisher.plan import validate_plan


@pytest.fixture(scope="module")
def deck(tmp_path_factory):
    return extract_deck(build_rich_deck(tmp_path_factory.mktemp("pl") / "rich.pptx"))


# --- cost -----------------------------------------------------------------

def test_cost_uses_per_model_rates():
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=0, cache_write_tokens=0)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(2.0 + 1.0)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(4.0 + 2.0)


def test_cost_prices_cache_reads_and_writes():
    usage = Usage(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost_usd("claude-sonnet-5", usage) == pytest.approx(0.20 + 2.50)


def test_cost_for_unknown_model_raises():
    with pytest.raises(KeyError):
        cost_usd("gpt-9", Usage(1, 1, 0, 0))


# --- planner input --------------------------------------------------------

def test_planner_input_lists_every_plannable_id_and_no_binaries(deck):
    data = planner_input(deck)
    ids = [item["id"] for s in data["slides"] for item in s["items"]]
    assert len(ids) == 17
    text = json.dumps(data)
    assert "blob" not in text and "chart_xml" not in text


def test_planner_input_summarises_visuals(deck):
    items = {i["id"]: i for s in planner_input(deck)["slides"] for i in s["items"]}
    assert items["s3-tb1"]["rows"] == 4 and items["s3-tb1"]["cols"] == 3
    assert items["s4-c1"]["series"] == ["2025", "2026"]
    assert items["s5-i1"]["type"] == "image"
    assert items["s2-t3"]["level"] == 1


def test_system_prompt_is_long_enough_to_cache():
    # Sonnet 5 only caches prefixes of 1024+ tokens (~4 chars per token)
    assert len(SYSTEM_PROMPT) > 1024 * 4


# --- Claude planner with a fake provider ----------------------------------

class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return JsonResult(data=r, usage=Usage(1000, 500, 0, 0), model=kwargs["model"], duration_ms=5)


def structured(slides):
    """A plan in the structured-output shape Claude returns."""
    out = []
    for s in slides:
        slots = [{"name": k, "ids": v} for k, v in s["slots"].items() if k != "stats"]
        out.append({"source_slide": s["source_slide"], "layout": s["layout"], "slots": slots,
                    "stats": s["slots"].get("stats", [])})
    return {"slides": out}


GOOD = [
    {"source_slide": 1, "layout": "title", "slots": {"title": ["s1-t1"], "subtitle": ["s1-t2"]}},
    {"source_slide": 2, "layout": "stats", "slots": {
        "title": ["s2-t1"], "stats": [{"ids": ["s2-t2"], "highlight": "Revenue grew 45%"}], "body": ["s2-t3", "s2-t4"]}},
    {"source_slide": 3, "layout": "table", "slots": {"title": ["s3-t1"], "visuals": ["s3-tb1"]}},
    {"source_slide": 4, "layout": "chart", "slots": {"title": ["s4-t1"], "visuals": ["s4-c1"]}},
    {"source_slide": 5, "layout": "image_text", "slots": {"title": ["s5-t1"], "visuals": ["s5-i1"]}},
    {"source_slide": 6, "layout": "chart", "slots": {"title": ["s6-t1"], "visuals": ["s6-c1"], "body": ["s6-t2"]}},
    {"source_slide": 7, "layout": "title", "slots": {"title": ["s7-t1"], "subtitle": ["s7-t2"]}},
]


def test_claude_planner_turns_structured_output_into_a_plan(deck):
    provider = FakeProvider([structured(GOOD)])
    attempt = ClaudePlanner(provider, model="claude-sonnet-5").plan(deck)
    assert attempt.error is None
    assert validate_plan(attempt.plan, deck) == []
    stats = attempt.plan.slides[1].slots["stats"]
    assert stats[0].ids == ["s2-t2"] and stats[0].highlight == "Revenue grew 45%"
    assert attempt.cost_usd == pytest.approx(cost_usd("claude-sonnet-5", Usage(1000, 500, 0, 0)))


def test_claude_planner_sends_system_prompt_schema_and_deck(deck):
    provider = FakeProvider([structured(GOOD)])
    ClaudePlanner(provider, model="claude-sonnet-5", effort="medium").plan(deck)
    call = provider.calls[0]
    assert call["system"] == SYSTEM_PROMPT
    assert call["schema"] == PLAN_SCHEMA
    assert call["effort"] == "medium"
    assert "s2-t2" in call["user"]


def test_feedback_goes_into_the_request(deck):
    provider = FakeProvider([structured(GOOD)])
    ClaudePlanner(provider, model="claude-sonnet-5").plan(deck, feedback=["Missing text [s2-t3]: 'x'"])
    assert "Missing text [s2-t3]" in provider.calls[0]["user"]


def test_provider_error_becomes_a_failed_attempt(deck):
    provider = FakeProvider([ProviderError("refusal", retryable=False)])
    attempt = ClaudePlanner(provider, model="claude-sonnet-5").plan(deck)
    assert attempt.plan is None
    assert "refusal" in attempt.error


def test_malformed_output_becomes_a_failed_attempt(deck):
    provider = FakeProvider([{"pages": []}])
    attempt = ClaudePlanner(provider, model="claude-sonnet-5").plan(deck)
    assert attempt.plan is None and attempt.error


def test_schema_objects_all_forbid_additional_properties():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(PLAN_SCHEMA)


# --- the real provider's request shape (fake SDK client) ------------------

class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, message):
        self.kwargs = None
        self.messages = SimpleNamespace(stream=self._stream)
        self._message = message

    def _stream(self, **kwargs):
        self.kwargs = kwargs
        return FakeStream(self._message)


def fake_message(text, stop_reason="end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model="claude-sonnet-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=20, cache_read_input_tokens=5,
                              cache_creation_input_tokens=None),
        _request_id="req_1",
    )


def test_anthropic_provider_request_shape():
    client = FakeClient(fake_message('{"slides": []}'))
    result = AnthropicProvider(client=client).complete_json(
        model="claude-sonnet-5", system="SYS", user="USER", schema={"type": "object"}, effort="medium"
    )
    kw = client.kwargs
    assert kw["model"] == "claude-sonnet-5"
    assert kw["system"] == [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]
    assert kw["thinking"] == {"type": "adaptive"}
    assert kw["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "temperature" not in kw and "tool_choice" not in kw
    assert result.data == {"slides": []}
    assert (result.usage.input_tokens, result.usage.cache_read_tokens, result.usage.cache_write_tokens) == (10, 5, 0)


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_anthropic_provider_rejects_unusable_stops(stop_reason):
    client = FakeClient(fake_message('{"slides": [', stop_reason=stop_reason))
    with pytest.raises(ProviderError) as exc:
        AnthropicProvider(client=client).complete_json(model="m", system="s", user="u", schema={}, effort="low")
    assert stop_reason in str(exc.value)
    assert exc.value.usage is not None  # the tokens were still spent and must be logged


def test_system_prompt_describes_the_new_layouts():
    assert "### agenda" in SYSTEM_PROMPT and "### timeline" in SYSTEM_PROMPT and "### image_full" in SYSTEM_PROMPT
    assert "three times in a row" in SYSTEM_PROMPT
