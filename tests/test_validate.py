import shutil
import zipfile

import pytest

from deckbuilder import add_zip_member, build_simple_deck, cfb_bytes
from polisher.validate import ValidationError, validate_file


@pytest.fixture
def simple(tmp_path):
    return build_simple_deck(tmp_path / "deck.pptx", n_slides=3)


def assert_rejected(path, code, **kw):
    with pytest.raises(ValidationError) as exc:
        validate_file(path, **kw)
    assert exc.value.code == code
    assert exc.value.message  # always a human-readable message for the UI
    return exc.value


def test_valid_deck_reports_slide_count(simple):
    info = validate_file(simple)
    assert info.slide_count == 3


def test_rejects_non_pptx_extension(tmp_path, simple):
    pdf = tmp_path / "deck.pdf"
    shutil.copy(simple, pdf)
    assert_rejected(pdf, "not_pptx")


def test_rejects_pptm_extension(tmp_path, simple):
    pptm = tmp_path / "deck.pptm"
    shutil.copy(simple, pptm)
    assert_rejected(pptm, "macro_enabled")


def test_rejects_macros_hidden_in_pptx(tmp_path, simple):
    sneaky = add_zip_member(simple, tmp_path / "sneaky.pptx", "ppt/vbaProject.bin", b"\x00" * 10)
    assert_rejected(sneaky, "macro_enabled")


def test_rejects_file_over_size_limit(simple):
    assert_rejected(simple, "too_large", max_bytes=1000)


def test_rejects_too_many_slides(tmp_path):
    deck = build_simple_deck(tmp_path / "big.pptx", n_slides=6)
    err = assert_rejected(deck, "too_many_slides", max_slides=5)
    assert "6" in err.message


def test_rejects_random_bytes(tmp_path):
    junk = tmp_path / "junk.pptx"
    junk.write_bytes(b"this is not a zip file at all" * 10)
    assert_rejected(junk, "corrupt")


def test_rejects_password_protected(tmp_path):
    locked = tmp_path / "locked.pptx"
    locked.write_bytes(cfb_bytes(with_encrypted_package=True))
    assert_rejected(locked, "password_protected")


def test_rejects_legacy_ppt_binary(tmp_path):
    old = tmp_path / "old.pptx"
    old.write_bytes(cfb_bytes(with_encrypted_package=False))
    assert_rejected(old, "legacy_ppt")


def test_rejects_ppt_extension_as_legacy(tmp_path):
    old = tmp_path / "old.ppt"
    old.write_bytes(cfb_bytes(with_encrypted_package=False))
    assert_rejected(old, "legacy_ppt")


def test_rejects_zip_that_is_not_a_presentation(tmp_path):
    fake = tmp_path / "fake.pptx"
    with zipfile.ZipFile(fake, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<doc/>")
    assert_rejected(fake, "not_pptx")


def test_rejects_zip_bomb(simple):
    assert_rejected(simple, "corrupt", max_uncompressed_bytes=1000)


def test_rejects_deck_with_no_slides(tmp_path):
    from pptx import Presentation

    empty = tmp_path / "empty.pptx"
    Presentation().save(empty)
    assert_rejected(empty, "empty")


# --- Amendment 1D: 25 MB normally, 100 MB with video or audio ---------------------

def _video_deck(tmp_path):
    from lockedbuilder import build_locked_deck

    return build_locked_deck(tmp_path / "video.pptx")


def test_default_limits_are_25_and_100_mb():
    from polisher.validate import MAX_BYTES, MAX_BYTES_MEDIA

    assert (MAX_BYTES, MAX_BYTES_MEDIA) == (25 * 1024 * 1024, 100 * 1024 * 1024)


def test_media_deck_may_exceed_the_normal_limit(tmp_path):
    deck = _video_deck(tmp_path)
    info = validate_file(deck, max_bytes=1000, max_bytes_media=10_000_000)
    assert info.has_media


def test_media_deck_over_the_media_limit_is_rejected(tmp_path):
    err = assert_rejected(_video_deck(tmp_path), "too_large", max_bytes=1000, max_bytes_media=2000)
    assert "video or audio" in err.message


def test_deck_without_media_keeps_the_normal_limit(simple):
    err = assert_rejected(simple, "too_large", max_bytes=1000, max_bytes_media=10_000_000)
    assert "Compress Pictures" in err.message
