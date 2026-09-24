"""Structured content of a deck. Every atomic piece of content has a stable ID.

IDs look like ``s3-t2`` (slide 3, text item 2). Kinds: t = text paragraph,
tb = table, c = chart, i = image, l = locked object, bg = picture background.
Numbering follows reading order. ``shape_id`` is the source shape's cNvPr id,
which the in-place renderer uses to find the element again.
"""

from dataclasses import asdict, dataclass, field


@dataclass
class Run:
    text: str
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    href: str | None = None
    br: bool = False  # a soft line break inside the paragraph (text is "\v")


@dataclass
class TextItem:
    id: str
    slide: int
    role: str  # title | subtitle | body | footer
    text: str
    runs: list[Run]
    level: int = 0
    bullet: str | None = None  # None | "char" | "number"
    number_type: str | None = None  # e.g. "arabicPeriod" when bullet == "number"
    box: str = ""  # paragraphs from the same source text box share this
    shape_id: int = 0
    kind: str = "text"


@dataclass
class TableCell:
    row: int
    col: int
    text: str
    bold: bool = False
    row_span: int = 1
    col_span: int = 1
    spanned: bool = False  # covered by another cell's merge


@dataclass
class TableItem:
    id: str
    slide: int
    rows: int
    cols: int
    cells: list[list[TableCell]]
    col_widths: list[float]  # fractions of total width
    header_row: bool = True
    pos: tuple = (0.0, 0.0, 1.0, 1.0)
    shape_id: int = 0
    kind: str = "table"

    def cell(self, row, col):
        return self.cells[row][col]


@dataclass
class ChartSeries:
    name: str
    values: list  # floats or None
    x_values: list | None = None  # XY and bubble charts
    sizes: list | None = None  # bubble charts
    number_format: str | None = None


@dataclass
class ChartItem:
    id: str
    slide: int
    chart_type: str  # XL_CHART_TYPE member name, e.g. "COLUMN_CLUSTERED"
    categories: list[str]
    series: list[ChartSeries]
    title: str | None = None
    x_axis_title: str | None = None
    y_axis_title: str | None = None
    has_legend: bool = False
    # "native" = rebuild with python-pptx; "keep" = keep the original chart shape and move it
    # (combo charts, multi-level categories, types python-pptx can't build)
    rebuild: str = "native"
    pos: tuple = (0.0, 0.0, 1.0, 1.0)
    shape_id: int = 0
    kind: str = "chart"
    chart_xml: bytes | None = field(default=None, repr=False)
    workbook_blob: bytes | None = field(default=None, repr=False)


@dataclass
class ImageItem:
    id: str
    slide: int
    sha256: str
    content_type: str
    ext: str
    px_width: int
    px_height: int
    crop: tuple = (0.0, 0.0, 0.0, 0.0)  # left, top, right, bottom fractions
    crop_raw: dict = field(default_factory=dict)  # a:srcRect attributes exactly as stored (1/1000 %)
    display_aspect: float = 1.0  # width / height as shown on the original slide
    alt_text: str = ""  # cNvPr descr
    title: str = ""  # cNvPr title
    svg_sha256: str = ""  # the SVG part kept alongside the PNG fallback, if any
    pos: tuple = (0.0, 0.0, 1.0, 1.0)
    shape_id: int = 0
    kind: str = "image"
    blob: bytes = field(default=b"", repr=False)


@dataclass
class LockedItem:
    """An object the renderer must keep untouched (Amendment 1C). Only position and size may change."""

    id: str
    slide: int
    lock_type: str  # video | audio | ole | equation | model3d | smartart | ink | group | linked_image | other
    shape_id: int
    pos: tuple = (0.0, 0.0, 1.0, 1.0)
    aspect: float = 1.0
    part_hashes: list = field(default_factory=list)  # SHA-256 of every related part, sorted
    links: list = field(default_factory=list)  # external targets (linked files)
    equations: int = 0
    has_media: bool = False  # video or audio (itself, or inside a locked group)
    xml_sig: str = ""  # SHA-256 of the element XML with positions and relationship IDs removed
    name: str = ""
    kind: str = "locked"


@dataclass
class BackgroundItem:
    """A slide's picture background (Amendment 1E)."""

    id: str
    slide: int
    sha256: str
    aspect: float = 1.0
    kind: str = "background"


@dataclass
class MasterImage:
    container: str  # "master:1", "layout:3"
    sha256: str
    name: str = ""


@dataclass
class SlideContent:
    number: int  # 1-based
    texts: list[TextItem] = field(default_factory=list)
    tables: list[TableItem] = field(default_factory=list)
    charts: list[ChartItem] = field(default_factory=list)
    images: list[ImageItem] = field(default_factory=list)
    locked: list[LockedItem] = field(default_factory=list)
    backgrounds: list[BackgroundItem] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # all item IDs in reading order
    box_pos: dict = field(default_factory=dict)  # box id -> (x, y, w, h) fractions
    notes: str = ""
    hidden: bool = False

    def items(self):
        by_id = {x.id: x for x in (*self.texts, *self.tables, *self.charts, *self.images, *self.locked,
                                   *self.backgrounds)}
        return [by_id[i] for i in self.order]


@dataclass
class DeckContent:
    slides: list[SlideContent]
    slide_width: int
    slide_height: int
    unsupported: list[str] = field(default_factory=list)
    master_images: list[MasterImage] = field(default_factory=list)

    def item(self, item_id):
        return self._index()[item_id]

    def all_items(self):
        return [x for s in self.slides for x in s.items()]

    def _index(self):
        if not hasattr(self, "_idx"):
            self._idx = {x.id: x for x in self.all_items()}
        return self._idx

    def linked_objects(self):
        return [x for s in self.slides for x in s.locked if x.links]

    def locked_summary(self):
        """What the quote screen tells the customer about locked objects."""
        counts = {}
        slides = set()
        for s in self.slides:
            for x in s.locked:
                counts[x.lock_type] = counts.get(x.lock_type, 0) + 1
                slides.add(s.number)
        has_media = any(x.has_media for s in self.slides for x in s.locked)
        return {
            "counts": counts,
            "slides": len(slides),
            "linked": len(self.linked_objects()),
            "has_media": has_media,
            "equations": sum(x.equations for s in self.slides for x in s.locked),
            "model3d": counts.get("model3d", 0),
        }

    def to_dict(self):
        """JSON-safe dict without binary blobs."""
        return _strip_binary(asdict(self))


BINARY_FIELDS = {"blob", "chart_xml", "workbook_blob"}


def _strip_binary(obj):
    if isinstance(obj, dict):
        return {k: _strip_binary(v) for k, v in obj.items() if k not in BINARY_FIELDS and not k.startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [_strip_binary(v) for v in obj]
    return obj
