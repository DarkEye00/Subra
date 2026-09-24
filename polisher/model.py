"""Structured content of a deck. Every atomic piece of content has a stable ID.

IDs look like ``s3-t2`` (slide 3, text item 2). Kinds: t = text paragraph,
tb = table, c = chart, i = image. Numbering follows reading order.
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
    # "native" = rebuild with python-pptx; "clone" = copy the original chart part
    # (combo charts, multi-level categories, types python-pptx can't build)
    rebuild: str = "native"
    pos: tuple = (0.0, 0.0, 1.0, 1.0)
    kind: str = "chart"
    chart_xml: bytes | None = field(default=None, repr=False)
    workbook_blob: bytes | None = field(default=None, repr=False)


@dataclass
class ImageItem:
    id: str
    slide: int
    sha1: str
    content_type: str
    ext: str
    px_width: int
    px_height: int
    crop: tuple = (0.0, 0.0, 0.0, 0.0)  # left, top, right, bottom fractions
    display_aspect: float = 1.0  # width / height as shown on the original slide
    alt_text: str = ""
    pos: tuple = (0.0, 0.0, 1.0, 1.0)
    kind: str = "image"
    blob: bytes = field(default=b"", repr=False)


@dataclass
class SlideContent:
    number: int  # 1-based
    texts: list[TextItem] = field(default_factory=list)
    tables: list[TableItem] = field(default_factory=list)
    charts: list[ChartItem] = field(default_factory=list)
    images: list[ImageItem] = field(default_factory=list)
    order: list[str] = field(default_factory=list)  # all item IDs in reading order
    box_pos: dict = field(default_factory=dict)  # box id -> (x, y, w, h) fractions
    notes: str = ""
    hidden: bool = False

    def items(self):
        by_id = {x.id: x for x in (*self.texts, *self.tables, *self.charts, *self.images)}
        return [by_id[i] for i in self.order]


@dataclass
class DeckContent:
    slides: list[SlideContent]
    slide_width: int
    slide_height: int
    unsupported: list[str] = field(default_factory=list)

    def item(self, item_id):
        return self._index()[item_id]

    def all_items(self):
        return [x for s in self.slides for x in s.items()]

    def _index(self):
        if not hasattr(self, "_idx"):
            self._idx = {x.id: x for x in self.all_items()}
        return self._idx

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
