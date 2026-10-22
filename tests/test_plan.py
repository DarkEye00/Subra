import copy

import pytest

from deckbuilder import build_rich_deck
from polisher.extract import extract_deck
from polisher.plan import PlanFormatError, parse_plan, plannable_ids, repair_plan, validate_plan

VALID = {
    "slides": [
        {"source_slide": 1, "layout": "title", "slots": {"title": ["s1-t1"], "subtitle": ["s1-t2"]}},
        {"source_slide": 2, "layout": "bullets", "slots": {"title": ["s2-t1"], "body": ["s2-t2", "s2-t3", "s2-t4"]}},
        {"source_slide": 3, "layout": "table", "slots": {"title": ["s3-t1"], "visuals": ["s3-tb1"]}},
        {"source_slide": 4, "layout": "chart", "slots": {"title": ["s4-t1"], "visuals": ["s4-c1"]}},
        {"source_slide": 5, "layout": "image_text", "slots": {"title": ["s5-t1"], "visuals": ["s5-i1"]}},
        {"source_slide": 6, "layout": "chart", "slots": {"title": ["s6-t1"], "visuals": ["s6-c1"], "body": ["s6-t2"]}},
        {"source_slide": 7, "layout": "quote", "slots": {"quote": ["s7-t1"], "attribution": ["s7-t2"]}},
    ]
}


@pytest.fixture(scope="module")
def deck(tmp_path_factory):
    return extract_deck(build_rich_deck(tmp_path_factory.mktemp("p") / "rich.pptx"))


def plan_with(mutate):
    data = copy.deepcopy(VALID)
    mutate(data["slides"])
    return parse_plan(data)


def errors_for(deck, mutate):
    return validate_plan(plan_with(mutate), deck)


def assert_error(errors, *needles):
    assert any(all(n in e for n in needles) for e in errors), errors


# --- parsing --------------------------------------------------------------

def test_parse_round_trips(deck):
    plan = parse_plan(VALID)
    assert plan.to_dict() == VALID


def test_parse_rejects_non_plan():
    with pytest.raises(PlanFormatError):
        parse_plan({"pages": []})


def test_stat_entries_are_parsed(deck):
    plan = parse_plan(
        {"slides": [{"source_slide": 2, "layout": "stats",
                     "slots": {"stats": [{"ids": ["s2-t2"], "highlight": "Revenue grew 45%"}]}}]}
    )
    (entry,) = plan.slides[0].slots["stats"]
    assert entry.ids == ["s2-t2"]
    assert entry.highlight == "Revenue grew 45%"


# --- validation -----------------------------------------------------------

def test_valid_plan_has_no_errors(deck):
    assert validate_plan(parse_plan(VALID), deck) == []


def test_plannable_ids_cover_everything_except_footers(deck):
    ids = plannable_ids(deck)
    assert "s3-tb1" in ids and "s5-i1" in ids and "s2-t4" in ids
    assert len(ids) == 17


def test_missing_id_is_an_error(deck):
    errors = errors_for(deck, lambda s: s[1]["slots"]["body"].remove("s2-t3"))
    assert_error(errors, "s2-t3", "missing")


def test_duplicate_id_is_an_error(deck):
    errors = errors_for(deck, lambda s: s[1]["slots"]["body"].append("s2-t3"))
    assert_error(errors, "s2-t3", "more than once")


def test_unknown_id_is_an_error(deck):
    errors = errors_for(deck, lambda s: s[1]["slots"]["body"].append("s2-t99"))
    assert_error(errors, "s2-t99", "unknown")


def test_unknown_layout_is_an_error(deck):
    errors = errors_for(deck, lambda s: s[1].update(layout="mosaic"))
    assert_error(errors, "mosaic")


def test_slot_not_in_layout_is_an_error(deck):
    def mutate(s):
        s[1]["slots"]["left"] = s[1]["slots"].pop("body")

    assert_error(errors_for(deck, mutate), "left", "bullets")


def test_wrong_kind_in_slot_is_an_error(deck):
    def mutate(s):
        s[3]["slots"]["body"] = s[3]["slots"].pop("visuals")

    errors = errors_for(deck, mutate)
    assert_error(errors, "s4-c1", "body")


def test_item_on_the_wrong_slide_is_an_error(deck):
    def mutate(s):
        s[1]["slots"]["body"].remove("s2-t4")
        s[2]["slots"]["body"] = ["s2-t4"]

    assert_error(errors_for(deck, mutate), "s2-t4", "slide 2")


def test_slides_out_of_order_is_an_error(deck):
    assert_error(errors_for(deck, lambda s: s.insert(0, s.pop(2))), "order")


def test_highlight_must_be_a_prefix_of_the_text(deck):
    def mutate(s):
        s[1] = {"source_slide": 2, "layout": "stats",
                "slots": {"title": ["s2-t1"], "stats": [{"ids": ["s2-t2"], "highlight": "45%"}],
                          "body": ["s2-t3", "s2-t4"]}}

    assert_error(errors_for(deck, mutate), "highlight", "s2-t2")


def test_too_many_stats_is_an_error(deck):
    def mutate(s):
        s[1] = {"source_slide": 2, "layout": "stats",
                "slots": {"stats": [{"ids": [i]} for i in ["s2-t1", "s2-t2", "s2-t3", "s2-t4"]]
                          + [{"ids": []}]}}

    assert_error(errors_for(deck, mutate), "stats", "at most 4")


def test_split_slide_is_valid(deck):
    def mutate(s):
        s[1]["slots"]["body"] = ["s2-t2", "s2-t3"]
        s.insert(2, {"source_slide": 2, "layout": "bullets", "slots": {"body": ["s2-t4"]}})

    assert errors_for(deck, mutate) == []


# --- repair ---------------------------------------------------------------

def repaired(deck, mutate):
    plan, repairs = repair_plan(plan_with(mutate), deck)
    assert validate_plan(plan, deck) == [], plan.to_dict()
    return plan, repairs


def test_repair_puts_missing_text_back_on_its_slide(deck):
    plan, repairs = repaired(deck, lambda s: s[1]["slots"]["body"].remove("s2-t3"))
    assert "s2-t3" in plan.slides[1].slots["body"]
    assert any("s2-t3" in r for r in repairs)


def test_repair_keeps_original_order_when_reinserting(deck):
    plan, _ = repaired(deck, lambda s: s[1]["slots"]["body"].remove("s2-t3"))
    assert plan.slides[1].slots["body"] == ["s2-t2", "s2-t3", "s2-t4"]


def test_repair_drops_duplicates_and_unknown_ids(deck):
    def mutate(s):
        s[1]["slots"]["body"] += ["s2-t3", "s2-t99"]

    plan, repairs = repaired(deck, mutate)
    assert plan.slides[1].slots["body"] == ["s2-t2", "s2-t3", "s2-t4"]
    assert len(repairs) == 2


def test_repair_moves_a_visual_out_of_a_text_slot(deck):
    def mutate(s):
        s[3] = {"source_slide": 4, "layout": "bullets", "slots": {"title": ["s4-t1"], "body": ["s4-c1"]}}

    plan, _ = repaired(deck, mutate)
    placed = [(p.layout, p.slots) for p in plan.slides if p.source_slide == 4]
    assert any("s4-c1" in slots.get("visuals", []) for _, slots in placed)


def test_repair_rebuilds_a_missing_slide(deck):
    plan, _ = repaired(deck, lambda s: s.pop(2))
    assert [p.source_slide for p in plan.slides] == [1, 2, 3, 4, 5, 6, 7]


def test_repair_replaces_unknown_layout(deck):
    plan, _ = repaired(deck, lambda s: s[1].update(layout="mosaic"))
    assert plan.slides[1].layout == "bullets"


def test_repair_drops_invalid_highlight(deck):
    def mutate(s):
        s[1] = {"source_slide": 2, "layout": "stats",
                "slots": {"title": ["s2-t1"], "stats": [{"ids": ["s2-t2"], "highlight": "45%"}],
                          "body": ["s2-t3", "s2-t4"]}}

    plan, _ = repaired(deck, mutate)
    assert plan.slides[1].slots["stats"][0].highlight is None


def test_repair_reorders_slides(deck):
    plan, _ = repaired(deck, lambda s: s.insert(0, s.pop(2)))
    assert [p.source_slide for p in plan.slides] == [1, 2, 3, 4, 5, 6, 7]


def test_repair_moves_item_back_to_its_own_slide(deck):
    def mutate(s):
        s[1]["slots"]["body"].remove("s2-t4")
        s[2]["slots"]["body"] = ["s2-t4"]

    plan, _ = repaired(deck, mutate)
    assert "s2-t4" in plan.slides[1].slots["body"]


def test_highlight_must_end_at_a_word_boundary(deck):
    def mutate(s):
        s[1] = {"source_slide": 2, "layout": "stats",
                "slots": {"title": ["s2-t1"], "stats": [{"ids": ["s2-t2"], "highlight": "Revenue gr"}],
                          "body": ["s2-t3", "s2-t4"]}}

    assert_error(errors_for(deck, mutate), "highlight", "s2-t2")
