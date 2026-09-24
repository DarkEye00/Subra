"""Step 1: reject anything we can't safely process, before the user pays."""

import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

MAX_BYTES = 25 * 1024 * 1024
MAX_BYTES_MEDIA = 100 * 1024 * 1024  # decks with video or audio (Amendment 1D)
MAX_SLIDES = 50
# Guard against zip bombs: even a 100 MB media deck should never expand beyond this.
MAX_UNCOMPRESSED_BYTES = 600 * 1024 * 1024
MAX_ZIP_ENTRIES = 5000

CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
PRESENTATION_CT = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
MACRO_CT = "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"

MESSAGES = {
    "not_pptx": "Please upload a PowerPoint file saved as .pptx.",
    "macro_enabled": "This file contains macros. Please save it as a regular .pptx (without macros) and upload again.",
    "legacy_ppt": "This is an older PowerPoint format. Open it in PowerPoint, choose Save As > .pptx, and upload again.",
    "password_protected": "This file is password-protected. Please remove the password and upload again.",
    "corrupt": "We couldn't read this file. It may be damaged. Try opening and re-saving it in PowerPoint.",
    "empty": "This presentation has no slides.",
}


class ValidationError(Exception):
    def __init__(self, code, message=None):
        self.code = code
        self.message = message or MESSAGES[code]
        super().__init__(f"{code}: {self.message}")


@dataclass
class DeckInfo:
    slide_count: int
    size_bytes: int
    has_media: bool = False


def _mb(n):
    return f"{n / 1024 / 1024:.0f} MB"


def _too_large(size, limit, media):
    if media:
        return ValidationError(
            "too_large",
            f"This file is {size / 1024 / 1024:.1f} MB. Decks with video or audio can be up to {_mb(limit)}. "
            "Try compressing the media in PowerPoint (File > Info > Compress Media).",
        )
    return ValidationError(
        "too_large",
        f"This file is {size / 1024 / 1024:.1f} MB. The limit is {_mb(limit)} "
        "(more for decks with video or audio). Try compressing images in PowerPoint (File > Compress Pictures).",
    )


def _has_media(content_types_xml):
    root = etree.fromstring(content_types_xml, etree.XMLParser(resolve_entities=False, no_network=True))
    return any(
        (el.get("ContentType") or "").startswith(("video/", "audio/")) for el in root
    )


def validate_file(
    path,
    max_bytes=MAX_BYTES,
    max_slides=MAX_SLIDES,
    max_uncompressed_bytes=MAX_UNCOMPRESSED_BYTES,
    max_bytes_media=MAX_BYTES_MEDIA,
):
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".pptm", ".potm", ".ppsm"):
        raise ValidationError("macro_enabled")
    if ext == ".ppt":
        raise ValidationError("legacy_ppt")
    if ext != ".pptx":
        raise ValidationError("not_pptx")

    size = path.stat().st_size
    if size > max(max_bytes, max_bytes_media):
        raise _too_large(size, max_bytes_media, media=True)

    with open(path, "rb") as f:
        head = f.read(8)
    if head == CFB_MAGIC:
        # OLE compound file: either an encrypted OOXML package or a legacy .ppt
        data = path.read_bytes()
        if "EncryptedPackage".encode("utf-16-le") in data:
            raise ValidationError("password_protected")
        raise ValidationError("legacy_ppt")

    if not zipfile.is_zipfile(path):
        raise ValidationError("corrupt")

    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                raise ValidationError("corrupt")
            if sum(i.file_size for i in infos) > max_uncompressed_bytes:
                raise ValidationError("corrupt")
            names = {i.filename for i in infos}
            if "[Content_Types].xml" not in names:
                raise ValidationError("not_pptx")
            content_types_xml = z.read("[Content_Types].xml")
            content_types = content_types_xml.decode("utf-8", "replace")
            if MACRO_CT in content_types or any(n.lower().endswith("vbaproject.bin") for n in names):
                raise ValidationError("macro_enabled")
            if PRESENTATION_CT not in content_types or "ppt/presentation.xml" not in names:
                raise ValidationError("not_pptx")
            presentation_xml = z.read("ppt/presentation.xml")
    except zipfile.BadZipFile:
        raise ValidationError("corrupt")

    try:
        root = etree.fromstring(presentation_xml, etree.XMLParser(resolve_entities=False, no_network=True))
        has_media = _has_media(content_types_xml)
    except etree.XMLSyntaxError:
        raise ValidationError("corrupt")
    if has_media and size > max_bytes_media:
        raise _too_large(size, max_bytes_media, media=True)
    if not has_media and size > max_bytes:
        raise _too_large(size, max_bytes, media=False)
    slide_count = len(root.xpath("./*[local-name()='sldIdLst']/*[local-name()='sldId']"))
    if slide_count == 0:
        raise ValidationError("empty")
    if slide_count > max_slides:
        raise ValidationError(
            "too_many_slides",
            f"This deck has {slide_count} slides. We currently polish decks of up to {max_slides} slides. "
            "Try splitting it into smaller decks.",
        )

    # Final check: python-pptx must be able to open it.
    try:
        from pptx import Presentation

        Presentation(str(path))
    except Exception:
        raise ValidationError("corrupt")

    return DeckInfo(slide_count=slide_count, size_bytes=size, has_media=has_media)
