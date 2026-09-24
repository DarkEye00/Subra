"""Built-in themes (Amendment 1B: a colour scheme, a font pair, and background specs).

Themes are data, not template files. ``apply_theme`` rewrites a deck's own Office
theme (colours and fonts) so masters, layouts, kept charts, and anything the user
adds later in PowerPoint match the design. ``base_presentation`` gives a blank
16:9 deck in a theme, for future features that start from nothing (doc-to-deck).
"""

import re
from dataclasses import dataclass

from lxml import etree
from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.util import Emu, Inches

SLIDE_W_IN = 13.333
SLIDE_H_IN = 7.5
OLD_W_EMU = 9144000  # the default template is 4:3


@dataclass(frozen=True)
class Theme:
    slug: str
    name: str
    heading_font: str
    body_font: str
    bg: str  # content slide background
    text: str  # body text
    muted: str  # labels, captions, footers
    title: str  # slide titles on content slides
    primary: str  # cover and section backgrounds, table headers
    on_primary: str  # text on primary
    on_primary_muted: str  # secondary text on primary
    accent: str  # rules, bullets, decoration (not for small text)
    highlight: str  # big numbers and column headings (readable on bg)
    surface: str  # cards and table banding
    line: str  # dividers and gridlines
    chart: tuple  # series colours, in order
    decor: str  # "circles" | "bands" | "block"
    heading_bold: bool = True
    cover_on_primary: bool = True  # cover slide uses the primary background
    # Amendment 1E: a theme that replaces backgrounds turns picture backgrounds into
    # regular pictures (if the planner keeps them) or drops them (logged).
    replace_backgrounds: bool = True


THEMES = {
    t.slug: t
    for t in [
        Theme(
            slug="navy",
            name="Navy Executive",
            heading_font="Cambria",
            body_font="Calibri",
            bg="FFFFFF",
            text="1F2A37",
            muted="5B6776",
            title="0B1F3A",
            primary="0B1F3A",
            on_primary="FFFFFF",
            on_primary_muted="C3CEDC",
            accent="E0A526",
            highlight="0B1F3A",
            surface="F1F4F8",
            line="D5DCE5",
            chart=("0B1F3A", "E0A526", "3E7CB1", "8FA9C4", "6B7B8C", "B5651D"),
            decor="circles",
        ),
        Theme(
            slug="savanna",
            name="Savanna Warm",
            heading_font="Calibri",
            body_font="Calibri",
            bg="FBF7F1",
            text="2E2620",
            muted="75665A",
            title="7A2E1B",
            primary="8C3B24",
            on_primary="FFF8EF",
            on_primary_muted="F0D5C0",
            accent="E3A13B",
            highlight="A8431F",
            surface="F3EADF",
            line="E2D5C5",
            chart=("C0532F", "E3A13B", "5E7D4B", "8C3B24", "B89A7A", "3F5A6B"),
            decor="bands",
        ),
        Theme(
            slug="slate",
            name="Clean Slate",
            heading_font="Arial",
            body_font="Arial",
            bg="FFFFFF",
            text="1F2328",
            muted="667085",
            title="101828",
            primary="065F46",
            on_primary="FFFFFF",
            on_primary_muted="C7EBDD",
            accent="10B981",
            highlight="047857",
            surface="F2F4F7",
            line="E4E7EC",
            chart=("047857", "10B981", "0EA5E9", "F59E0B", "6366F1", "64748B"),
            decor="block",
            cover_on_primary=False,
        ),
    ]
}


def get_theme(slug):
    try:
        return THEMES[slug]
    except KeyError:
        raise ValueError(f"Unknown theme '{slug}'. Choose from: {', '.join(THEMES)}")


def mix(hex_a, hex_b, t):
    """Blend two hex colours: t=0 gives a, t=1 gives b."""
    a = [int(hex_a[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(hex_b[i:i + 2], 16) for i in (0, 2, 4)]
    return "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def base_presentation(theme):
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W_IN)
    prs.slide_height = Inches(SLIDE_H_IN)
    _widen_master_and_layouts(prs)
    apply_theme(prs, theme)
    return prs


def apply_theme(prs, theme):
    """Rewrite every master's theme colour and font scheme. Master and layout shapes are untouched."""
    seen = set()
    for master in prs.slide_masters:
        part = master.part.part_related_by(RT.THEME)
        if id(part) not in seen:
            seen.add(id(part))
            _patch_theme_part(part, theme)


def _widen_master_and_layouts(prs):
    """Scale every x offset and width in the master and layouts from 4:3 to 16:9."""
    factor = prs.slide_width / OLD_W_EMU
    trees = [prs.slide_master.element] + [layout.element for layout in prs.slide_layouts]
    for tree in trees:
        for off in tree.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}off"):
            off.set("x", str(int(int(off.get("x")) * factor)))
        for ext in tree.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}ext"):
            if ext.get("cx") is not None:
                ext.set("cx", str(int(int(ext.get("cx")) * factor)))


def _patch_theme_part(part, theme):
    xml = part.blob.decode("utf-8")
    colors = {
        "dk1": theme.text,
        "lt1": theme.bg,
        "dk2": theme.primary,
        "lt2": theme.surface,
        **{f"accent{i + 1}": c for i, c in enumerate(theme.chart[:6])},
        "hlink": theme.highlight,
        "folHlink": theme.muted,
    }
    for name, value in colors.items():
        xml = re.sub(
            rf"<a:{name}>.*?</a:{name}>",
            f'<a:{name}><a:srgbClr val="{value}"/></a:{name}>',
            xml,
            count=1,
            flags=re.S,
        )
    xml = re.sub(r'<a:clrScheme name="[^"]*"', f'<a:clrScheme name="SUBRA {theme.name}"', xml, count=1)
    xml = re.sub(r'<a:fontScheme name="[^"]*"', f'<a:fontScheme name="SUBRA {theme.name}"', xml, count=1)
    xml = re.sub(
        r'(<a:majorFont>\s*<a:latin typeface=")[^"]*(")', rf"\g<1>{theme.heading_font}\g<2>", xml, count=1
    )
    xml = re.sub(
        r'(<a:minorFont>\s*<a:latin typeface=")[^"]*(")', rf"\g<1>{theme.body_font}\g<2>", xml, count=1
    )
    etree.fromstring(xml.encode("utf-8"))  # fail loudly if the patch broke the XML
    if hasattr(part, "_element"):  # theme parts loaded from a file may be XML parts
        part._element = etree.fromstring(xml.encode("utf-8"))
    else:
        part._blob = xml.encode("utf-8")
