"""The layout library: the only slide designs the planner may choose from.

Each layout has named slots. A slot accepts certain kinds of content:
"text" (paragraph IDs), "visual" (table, chart, or image IDs), or "stat"
(stat entries, see plan.StatEntry).
"""

from dataclasses import dataclass

VISUAL_KINDS = frozenset({"table", "chart", "image"})


@dataclass(frozen=True)
class Slot:
    name: str
    accepts: str  # "text" | "visual" | "stat"
    description: str
    min_items: int = 0
    max_items: int | None = None


@dataclass(frozen=True)
class Layout:
    name: str
    description: str
    slots: tuple

    def slot(self, name):
        return next((s for s in self.slots if s.name == name), None)

    def slot_names(self):
        return [s.name for s in self.slots]

    def first_slot(self, accepts):
        return next((s for s in self.slots if s.accepts == accepts and s.name != "title"), None)


TITLE = Slot("title", "text", "The slide heading. Usually the original title paragraph(s).", max_items=3)

LAYOUTS = {
    layout.name: layout
    for layout in [
        Layout(
            "title",
            "Cover slide: large title with an optional subtitle (presenter, date, organisation). "
            "Use for the first slide and for other cover-style slides.",
            (TITLE, Slot("subtitle", "text", "Smaller lines under the title.")),
        ),
        Layout(
            "section",
            "Section divider: one big heading on a coloured background, with optional short subtitle. "
            "Use for slides that only introduce a new part of the deck.",
            (TITLE, Slot("subtitle", "text", "Optional short line under the section heading.")),
        ),
        Layout(
            "agenda",
            "Contents or outline slide: 3 to 7 items shown with large accent numbers, one paragraph per item. "
            "Use for agenda, contents, outline and 'what we'll cover' slides.",
            (TITLE, Slot("items", "text", "The items, in order. One paragraph each.", min_items=3, max_items=7)),
        ),
        Layout(
            "bullets",
            "Title plus a list of points. The default for text slides. Keeps paragraph levels and bullets.",
            (TITLE, Slot("body", "text", "The points, in original order.")),
        ),
        Layout(
            "two_column",
            "Title plus two side-by-side columns of text, each with an optional column heading. "
            "Use when the text naturally splits into two groups (compare/contrast, before/after, "
            "two text boxes side by side on the original slide).",
            (
                TITLE,
                Slot("left_title", "text", "Optional heading for the left column.", max_items=2),
                Slot("left", "text", "Left column text."),
                Slot("right_title", "text", "Optional heading for the right column.", max_items=2),
                Slot("right", "text", "Right column text."),
            ),
        ),
        Layout(
            "stats",
            "Title plus 1-4 key numbers shown large in cards, each with a short label. "
            "Use when a slide's point is a few headline figures.",
            (
                TITLE,
                Slot("stats", "stat", "One entry per card.", min_items=1, max_items=4),
                Slot("body", "text", "Optional supporting text shown below the cards."),
            ),
        ),
        Layout(
            "table",
            "Title plus a full-width native table (or up to 2 visuals stacked), with optional text below.",
            (
                TITLE,
                Slot("visuals", "visual", "The table(s).", min_items=1, max_items=2),
                Slot("body", "text", "Optional short text shown under the table."),
            ),
        ),
        Layout(
            "chart",
            "Title plus 1-2 native charts (or other visuals) with optional text in a side panel.",
            (
                TITLE,
                Slot("visuals", "visual", "The chart(s).", min_items=1, max_items=2),
                Slot("body", "text", "Optional explanatory text, shown beside the chart."),
            ),
        ),
        Layout(
            "image_text",
            "Title plus 1-4 images on one side and text on the other. With no text, images fill the slide.",
            (
                TITLE,
                Slot("visuals", "visual", "The image(s).", min_items=1, max_items=4),
                Slot("body", "text", "Text shown beside the images."),
            ),
        ),
        Layout(
            "image_full",
            "One photo or other visual fills the slide, with the title on a band along the bottom. "
            "Use when a slide is a single large picture plus a title and nothing else.",
            (TITLE, Slot("visuals", "visual", "The one picture.", min_items=1, max_items=1)),
        ),
        Layout(
            "timeline",
            "Title plus 3 to 6 steps or dates on a horizontal line, one paragraph per node. "
            "Use for roadmaps, milestones, project phases and histories.",
            (TITLE, Slot("steps", "text", "The steps, in order. One paragraph each.", min_items=3, max_items=6)),
        ),
        Layout(
            "quote",
            "A single large statement: a quotation, key message, or closing/thank-you slide, "
            "with optional attribution or contact line.",
            (
                Slot("quote", "text", "The statement.", min_items=1),
                Slot("attribution", "text", "Optional speaker, source, or contact details."),
            ),
        ),
    ]
}

# Used only for original slides with no plannable content (keeps slide order and notes).
BLANK = Layout("blank", "Empty slide.", ())
