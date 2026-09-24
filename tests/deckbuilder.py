"""Builders for synthetic test decks.

Real customer decks are the real test (see PROGRESS.md), but these cover the
structures the extractor has to handle: placeholders, bullets with levels,
runs, tables with merges, charts, cropped images, groups, notes, hidden slides.
"""

import io
import zipfile

from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches, Pt


def png_bytes(width=400, height=300, color=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def build_rich_deck(path):
    """A 7-slide deck exercising every content type we extract."""
    prs = Presentation()

    # 1. Title slide: title + subtitle placeholders
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Quarterly Business Review"
    s.placeholders[1].text = "Nairobi office, Q3 2026"
    s.notes_slide.notes_text_frame.text = "Welcome everyone.\nKeep intro under 2 minutes."

    # 2. Bullets with levels and mixed runs
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Highlights"
    tf = s.placeholders[1].text_frame
    tf.text = "Revenue grew 45% to KES 12.5m"
    p = tf.add_paragraph()
    p.text = "Driven by M-Pesa collections"
    p.level = 1
    p = tf.add_paragraph()
    r1 = p.add_run()
    r1.text = "Churn: "
    r1.font.bold = True
    r2 = p.add_run()
    r2.text = "down to 3.2%"
    r2.font.italic = True
    s.notes_slide.notes_text_frame.text = "Mention the Mombasa launch."

    # 3. Table with a merged header cell
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Regional sales"
    rows, cols = 4, 3
    tbl = s.shapes.add_table(rows, cols, Inches(0.5), Inches(1.5), Inches(9), Inches(3)).table
    data = [
        ["Region", "2025", "2026"],
        ["Nairobi", "1,200", "1,850"],
        ["Mombasa", "640", "910"],
        ["Kisumu", "300", "415"],
    ]
    for r in range(rows):
        for c in range(cols):
            tbl.cell(r, c).text = data[r][c]
    tbl.cell(0, 1).merge(tbl.cell(0, 2))
    tbl.cell(0, 1).text = "Units sold"

    # 4. Bar chart with two series
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Growth by quarter"
    cd = CategoryChartData()
    cd.categories = ["Q1", "Q2", "Q3", "Q4"]
    cd.add_series("2025", (10.5, 12.0, 14.25, 15.0))
    cd.add_series("2026", (13.0, 16.5, 21.0, None))
    gf = s.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1.5), Inches(8), Inches(5), cd
    )
    gf.chart.has_title = True
    gf.chart.chart_title.text_frame.text = "Revenue (KES m)"

    # 5. Cropped image + caption text box
    s = prs.slides.add_slide(prs.slide_layouts[6])
    pic = s.shapes.add_picture(io.BytesIO(png_bytes(400, 300)), Inches(1), Inches(1), Inches(4))
    pic.crop_left = 0.1
    pic.crop_right = 0.1
    tb = s.shapes.add_textbox(Inches(6), Inches(1), Inches(3), Inches(1))
    tb.text_frame.text = "Our new Westlands branch"

    # 6. Group shape containing text, plus a scatter chart
    s = prs.slides.add_slide(prs.slide_layouts[6])
    grp = s.shapes.add_group_shape()
    a = grp.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
    a.text_frame.text = "Grouped note A"
    b = grp.shapes.add_textbox(Inches(1), Inches(2), Inches(3), Inches(1))
    b.text_frame.text = "Grouped note B"
    xy = XyChartData()
    ser = xy.add_series("Price vs volume")
    ser.add_data_point(1.5, 200)
    ser.add_data_point(2.0, 180)
    ser.add_data_point(3.25, 120)
    s.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Inches(5), Inches(1), Inches(4), Inches(4), xy)

    # 7. Hidden closing slide
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Thank you"
    s.placeholders[1].text = "questions@example.co.ke"
    s._element.set("show", "0")

    prs.save(path)
    return path


def build_simple_deck(path, n_slides=3):
    prs = Presentation()
    for i in range(n_slides):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = f"Slide {i + 1}"
        s.placeholders[1].text = f"Point {i + 1}"
    prs.save(path)
    return path


def add_zip_member(src, dst, name, data):
    """Copy a zip package and add one extra member (e.g. vbaProject.bin)."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            zout.writestr(item, zin.read(item.filename))
        zout.writestr(name, data)
    return dst


def cfb_bytes(with_encrypted_package):
    """Minimal bytes that look like an OLE compound file (legacy .ppt or encrypted OOXML)."""
    header = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504
    body = "EncryptedPackage".encode("utf-16-le") if with_encrypted_package else b"PowerPoint Document"
    return header + body + b"\x00" * 1024


def photo_bytes(width=1200, height=800, seed=0):
    """A smooth two-tone gradient with shapes, so previews look like a photo slot."""
    from PIL import ImageDraw

    palettes = [((32, 96, 64), (210, 180, 90)), ((20, 60, 110), (120, 190, 220)), ((120, 50, 30), (240, 200, 150))]
    a, b = palettes[seed % len(palettes)]
    img = Image.new("RGB", (width, height))
    px = img.load()
    for y in range(height):
        t = y / height
        row = tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))
        for x in range(width):
            px[x, y] = row
    d = ImageDraw.Draw(img)
    d.ellipse((width * 0.6, height * 0.1, width * 0.9, height * 0.55), fill=(250, 235, 200))
    d.polygon([(0, height), (width * 0.35, height * 0.45), (width * 0.7, height)], fill=tuple(c // 2 for c in a))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _bullets(tf, items):
    """items: list of str or (str, level)."""
    first = True
    for it in items:
        text, level = (it, 0) if isinstance(it, str) else it
        p = tf.paragraphs[0] if first else tf.add_paragraph()
        p.text = text
        p.level = level
        first = False


def build_business_deck(path):
    """A realistic 12-slide investor update used for visual QA of themes."""
    prs = Presentation()
    L_TITLE, L_CONTENT, L_TWO, L_ONLY, L_BLANK = (prs.slide_layouts[i] for i in (0, 1, 3, 5, 6))

    s = prs.slides.add_slide(L_TITLE)
    s.shapes.title.text = "Safari Fresh Foods"
    s.placeholders[1].text = "Investor update\vSeptember 2026"
    s.notes_slide.notes_text_frame.text = "Thank the board for attending."

    s = prs.slides.add_slide(L_CONTENT)
    s.shapes.title.text = "Agenda"
    _bullets(s.placeholders[1].text_frame, [
        "Where we are today", "Market opportunity", "Financial performance",
        "Operations and supply chain", "Plans for 2027",
    ])

    s = prs.slides.add_slide(L_CONTENT)
    s.shapes.title.text = "The market opportunity"
    _bullets(s.placeholders[1].text_frame, [
        "Urban households in Kenya spend a growing share of income on fresh produce, "
        "yet most still buy from informal markets with high spoilage.",
        "Cold-chain coverage outside Nairobi remains thin",
        ("Only 3 of 47 counties have commercial cold storage above 1,000 tonnes", 1),
        ("Farm-gate losses average 30% for leafy greens", 1),
        "Mobile ordering is now mainstream: 71% of our customers order via WhatsApp or USSD",
    ])

    s = prs.slides.add_slide(L_CONTENT)
    s.shapes.title.text = "2026 at a glance"
    _bullets(s.placeholders[1].text_frame, [
        "KES 450m revenue", "38% gross margin", "12 counties served", "2,400 farmers onboarded",
    ])

    s = prs.slides.add_slide(L_TWO)
    s.shapes.title.text = "What is working, and what is not"
    _bullets(s.placeholders[1].text_frame, [
        "Working", ("Repeat orders up 22%", 1), ("Farmer payments via M-Pesa within 24 hours", 1),
        ("Delivery cost per order down to KES 85", 1),
    ])
    _bullets(s.placeholders[2].text_frame, [
        "Needs fixing", ("Spoilage still 9% in Kisumu hub", 1), ("Driver churn above 40%", 1),
        ("Slow onboarding of corporate clients", 1),
    ])

    s = prs.slides.add_slide(L_ONLY)
    s.shapes.title.text = "Financial summary (KES millions)"
    data = [
        ["", "2023", "2024", "2025", "2026F"],
        ["Revenue", "180", "265", "340", "450"],
        ["Gross profit", "58", "93", "126", "171"],
        ["Operating costs", "(92)", "(110)", "(128)", "(150)"],
        ["EBITDA", "(34)", "(17)", "(2)", "21"],
        ["Cash at year end", "64", "120", "88", "140"],
    ]
    tbl = s.shapes.add_table(6, 5, Inches(0.5), Inches(1.5), Inches(9), Inches(4)).table
    for r, row in enumerate(data):
        for c, v in enumerate(row):
            tbl.cell(r, c).text = v

    s = prs.slides.add_slide(L_ONLY)
    s.shapes.title.text = "Monthly revenue trend"
    cd = CategoryChartData()
    cd.categories = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    cd.add_series("2025", (22, 24, 25, 27, 26, 28, 30, 31, 29, 32, 34, 36))
    cd.add_series("2026", (30, 31, 34, 35, 37, 38, 40, 41, 43, None, None, None))
    s.shapes.add_chart(XL_CHART_TYPE.LINE_MARKERS, Inches(0.5), Inches(1.5), Inches(6), Inches(4.5), cd)
    tb = s.shapes.add_textbox(Inches(6.7), Inches(2), Inches(3), Inches(3)).text_frame
    tb.text = "Growth accelerated after the Mombasa hub opened in March."
    tb.add_paragraph().text = "September was our best month ever."

    s = prs.slides.add_slide(L_ONLY)
    s.shapes.title.text = "Revenue by channel"
    cd = CategoryChartData()
    cd.categories = ["WhatsApp", "USSD", "Corporate", "Retail partners"]
    cd.add_series("Share", (0.46, 0.25, 0.18, 0.11))
    s.shapes.add_chart(XL_CHART_TYPE.PIE, Inches(1), Inches(1.5), Inches(8), Inches(5), cd)

    s = prs.slides.add_slide(L_ONLY)
    s.shapes.title.text = "Our new Mombasa hub"
    s.shapes.add_picture(io.BytesIO(photo_bytes(seed=1)), Inches(0.5), Inches(1.5), Inches(5))
    tb = s.shapes.add_textbox(Inches(5.8), Inches(1.6), Inches(3.8), Inches(4)).text_frame
    _bullets(tb, ["Opened March 2026", "1,200 tonnes cold storage", "Serves 4 coastal counties",
                  "Solar-powered, cutting energy costs by half"])

    s = prs.slides.add_slide(L_CONTENT)
    s.shapes.title.text = "Operations review"
    _bullets(s.placeholders[1].text_frame, [
        "Sourcing", ("2,400 farmers across 9 counties, 61% women", 1), ("Average farmer income up 18% year on year", 1),
        "Logistics", ("46 refrigerated vans, 12 motorbikes for last-mile delivery", 1),
        ("On-time delivery rate of 94%, target 97% by June 2027", 1),
        "Technology", ("Order platform handles 3,500 orders per day at peak", 1),
        ("Moved payments reconciliation to automated M-Pesa statements", 1),
        "People", ("186 full-time staff, 40% in operations", 1), ("New head of logistics joined in August", 1),
        "Risks", ("Fuel prices, county levies, and currency moves on imported packaging", 1),
    ])

    s = prs.slides.add_slide(L_CONTENT)
    s.shapes.title.text = "Next steps"
    tf = s.placeholders[1].text_frame
    _bullets(tf, ["Close KES 200m Series A by December", "Open Eldoret hub in Q2 2027",
                  "Launch B2B ordering portal", "Reach 5,000 farmers"])
    for p in tf.paragraphs:
        pPr = p._p.get_or_add_pPr()
        from pptx.oxml.ns import qn
        pPr.append(pPr.makeelement(qn("a:buAutoNum"), {"type": "arabicPeriod"}))

    s = prs.slides.add_slide(L_TITLE)
    s.shapes.title.text = "Thank you"
    s.placeholders[1].text = "Jane Wanjiku, CEO · jane@safarifresh.co.ke"

    prs.save(path)
    return path
