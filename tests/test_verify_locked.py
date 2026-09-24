"""Verification rules from Amendment 1C (locked objects) and 1E (images)."""

import copy

import pytest

from lockedbuilder import build_images_deck, build_locked_deck
from polisher.extract import extract_deck
from polisher.model import ImageItem, SlideContent
from polisher.verify import verify


@pytest.fixture(scope="module")
def locked(tmp_path_factory):
    return extract_deck(build_locked_deck(tmp_path_factory.mktemp("vl") / "locked.pptx"))


@pytest.fixture(scope="module")
def images(tmp_path_factory):
    return extract_deck(build_images_deck(tmp_path_factory.mktemp("vi") / "images.pptx"))


def identity_sources(deck):
    return [s.number for s in deck.slides]


def failed_with(report, needle):
    assert not report.passed
    assert any(needle in p for p in report.problems), report.problems


def test_untouched_locked_deck_passes(locked):
    assert verify(locked, copy.deepcopy(locked), slide_sources=identity_sources(locked)).passed


def test_missing_locked_object_fails(locked):
    out = copy.deepcopy(locked)
    out.slides[1].locked.clear()
    failed_with(verify(locked, out), "s2-l1")


def test_changed_part_hash_fails(locked):
    out = copy.deepcopy(locked)
    out.slides[3].locked[0].part_hashes = ["0" * 64] + out.slides[3].locked[0].part_hashes[1:]
    failed_with(verify(locked, out), "s4-l1")


def test_changed_locked_xml_fails(locked):
    out = copy.deepcopy(locked)
    out.slides[5].locked[0].xml_sig = "f" * 64
    failed_with(verify(locked, out), "s6-l1")


def test_locked_object_must_keep_its_shape_id(locked):
    out = copy.deepcopy(locked)
    out.slides[1].locked[0].shape_id += 100
    failed_with(verify(locked, out, slide_sources=identity_sources(locked)), "s2-l1")


def test_locked_object_must_stay_on_its_slide(locked):
    out = copy.deepcopy(locked)
    moved = out.slides[1].locked.pop()
    out.slides[2].locked.append(moved)
    failed_with(verify(locked, out, slide_sources=identity_sources(locked)), "s2-l1")


def test_equation_count_per_slide_must_match(locked):
    out = copy.deepcopy(locked)
    out.slides[5].locked[0].equations = 2
    failed_with(verify(locked, out, slide_sources=identity_sources(locked)), "equation")


def test_split_slides_count_toward_their_source(locked):
    out = copy.deepcopy(locked)
    out.slides.insert(6, SlideContent(number=99))
    sources = identity_sources(locked)
    sources.insert(6, 6)  # the extra slide was split from source slide 6
    assert verify(locked, out, slide_sources=sources).passed


# --- images -------------------------------------------------------------------

def test_untouched_images_deck_passes(images):
    assert verify(images, copy.deepcopy(images), slide_sources=identity_sources(images)).passed


def test_changed_crop_fails(images):
    out = copy.deepcopy(images)
    out.slides[1].images[0].crop_raw = {"l": 0, "t": 0, "r": 0, "b": 0}
    failed_with(verify(images, out), "s2-i1")


def test_changed_alt_text_fails(images):
    out = copy.deepcopy(images)
    out.slides[1].images[0].alt_text = ""
    failed_with(verify(images, out), "s2-i1")


def test_missing_svg_part_fails(images):
    out = copy.deepcopy(images)
    out.slides[3].images[1].svg_sha256 = ""
    failed_with(verify(images, out), "s4-i2")


def test_missing_master_logo_fails(images):
    out = copy.deepcopy(images)
    out.master_images = [m for m in out.master_images if m.container != "master:1"]
    failed_with(verify(images, out), "master:1")


def test_missing_background_fails(images):
    out = copy.deepcopy(images)
    out.slides[2].backgrounds.clear()
    failed_with(verify(images, out), "s3-bg1")


def test_background_kept_as_a_picture_passes(images):
    out = copy.deepcopy(images)
    bg = out.slides[2].backgrounds.pop()
    out.slides[2].images.append(ImageItem(id="x", slide=3, sha256=bg.sha256, content_type="image/jpeg", ext="jpg",
                                          px_width=800, px_height=600))
    assert verify(images, out).passed


def test_background_replaced_by_the_theme_is_allowed_when_logged(images):
    out = copy.deepcopy(images)
    out.slides[2].backgrounds.clear()
    assert verify(images, out, dropped_backgrounds=["s3-bg1"]).passed
