import pytest

from lockedbuilder import build_images_deck, build_locked_deck
from polisher.extract import extract_deck
from polisher.heuristic import plan_deck
from polisher.plan import parse_plan, plannable_ids, repair_plan, validate_plan
from polisher.planner import planner_input


@pytest.fixture(scope="module")
def locked(tmp_path_factory):
    return extract_deck(build_locked_deck(tmp_path_factory.mktemp("pl") / "locked.pptx"))


@pytest.fixture(scope="module")
def images(tmp_path_factory):
    return extract_deck(build_images_deck(tmp_path_factory.mktemp("pi") / "images.pptx"))


def test_heuristic_plans_locked_objects_as_visuals(locked):
    plan = plan_deck(locked)
    assert validate_plan(plan, locked) == []
    placed = {i for ps in plan.slides for i in ps.slots.get("visuals", [])}
    assert {x.id for s in locked.slides for x in s.locked} <= placed


def test_locked_objects_are_required(locked):
    data = plan_deck(locked).to_dict()
    data["slides"][1]["slots"]["visuals"].remove("s2-l1")
    errors = validate_plan(parse_plan(data), locked)
    assert any("s2-l1" in e and "missing" in e for e in errors)


def test_locked_object_must_stay_on_the_first_part_of_a_split(locked):
    data = plan_deck(locked).to_dict()
    first = data["slides"][1]
    first["slots"]["visuals"].remove("s2-l1")
    data["slides"].insert(2, {"source_slide": 2, "layout": "image_text", "slots": {"visuals": ["s2-l1"]}})
    errors = validate_plan(parse_plan(data), locked)
    assert any("s2-l1" in e and "first" in e for e in errors)
    repaired, _ = repair_plan(parse_plan(data), locked)
    assert validate_plan(repaired, locked) == []
    assert "s2-l1" in repaired.slides[1].ids()


def test_backgrounds_are_optional(images):
    assert "s3-bg1" in plannable_ids(images)
    plan = plan_deck(images)
    assert "s3-bg1" not in {i for ps in plan.slides for i in ps.ids()}
    assert validate_plan(plan, images) == []


def test_a_background_may_be_kept_as_a_picture(images):
    data = plan_deck(images).to_dict()
    data["slides"][2] = {"source_slide": 3, "layout": "image_text",
                         "slots": {"title": ["s3-t1"], "visuals": ["s3-bg1"], "body": ["s3-t2"]}}
    assert validate_plan(parse_plan(data), images) == []


def test_repair_does_not_re_add_dropped_backgrounds(images):
    plan, repairs = repair_plan(plan_deck(images), images)
    assert "s3-bg1" not in {i for ps in plan.slides for i in ps.ids()}
    assert not any("s3-bg1" in r for r in repairs)


def test_planner_sees_locked_objects_and_backgrounds_as_opaque_items(locked, images):
    items = {i["id"]: i for s in planner_input(locked)["slides"] for i in s["items"]}
    assert items["s2-l1"] == {"id": "s2-l1", "type": "locked", "object": "video", "aspect": pytest.approx(1.33, abs=0.01)}
    bg = {i["id"]: i for s in planner_input(images)["slides"] for i in s["items"]}["s3-bg1"]
    assert bg["type"] == "background" and "aspect" in bg
