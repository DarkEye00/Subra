"""Synthetic decks with every locked object type (Amendment 1C) and the image cases (1E).

python-pptx can only create video and embedded OLE objects, so audio, linked OLE,
equations, 3D models, SmartArt, ink, SVG pictures, picture backgrounds, and master
or layout logos are written here as raw XML and parts, following what PowerPoint
writes. They exercise the code paths; real PowerPoint files are still needed for
launch testing (see PROGRESS.md).
"""

import io

from pptx import Presentation
from pptx.enum.shapes import PROG_ID
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.package import Part
from pptx.opc.packuri import PackURI
from pptx.oxml import parse_xml
from pptx.util import Inches

from deckbuilder import photo_bytes, png_bytes

NSDECL = (
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
)
MODEL3D_RT = "http://schemas.microsoft.com/office/2017/06/relationships/model3d"
SVG_EXT_URI = "{96DAC541-7B7A-43D3-8B79-37D633B846F1}"

SVG_ICON = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    b'<circle cx="32" cy="32" r="28" fill="#0B1F3A"/><path d="M20 33l8 8 16-18" stroke="#E0A526" '
    b'stroke-width="6" fill="none"/></svg>'
)

SMARTART_DATA = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"
               xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <dgm:ptLst>
    <dgm:pt modelId="{00000000-0000-0000-0000-000000000000}" type="doc"><dgm:prSet/><dgm:spPr/><dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr lang="en-US"/></a:p></dgm:t></dgm:pt>
    <dgm:pt modelId="{00000000-0000-0000-0000-000000000001}"><dgm:prSet phldrT="[Text]"/><dgm:spPr/><dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/><a:t>Plan</a:t></a:r></a:p></dgm:t></dgm:pt>
    <dgm:pt modelId="{00000000-0000-0000-0000-000000000002}"><dgm:prSet phldrT="[Text]"/><dgm:spPr/><dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/><a:t>Build</a:t></a:r></a:p></dgm:t></dgm:pt>
    <dgm:pt modelId="{00000000-0000-0000-0000-000000000003}"><dgm:prSet phldrT="[Text]"/><dgm:spPr/><dgm:t><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"/><a:t>Launch</a:t></a:r></a:p></dgm:t></dgm:pt>
  </dgm:ptLst>
  <dgm:cxnLst>
    <dgm:cxn modelId="{10000000-0000-0000-0000-000000000001}" srcId="{00000000-0000-0000-0000-000000000000}" destId="{00000000-0000-0000-0000-000000000001}" srcOrd="0" destOrd="0"/>
    <dgm:cxn modelId="{10000000-0000-0000-0000-000000000002}" srcId="{00000000-0000-0000-0000-000000000000}" destId="{00000000-0000-0000-0000-000000000002}" srcOrd="1" destOrd="0"/>
    <dgm:cxn modelId="{10000000-0000-0000-0000-000000000003}" srcId="{00000000-0000-0000-0000-000000000000}" destId="{00000000-0000-0000-0000-000000000003}" srcOrd="2" destOrd="0"/>
  </dgm:cxnLst>
  <dgm:bg/><dgm:whole/>
</dgm:dataModel>"""


def _add_part(slide_or_part, partname, content_type, blob, reltype, external=False):
    part = getattr(slide_or_part, "part", slide_or_part)
    if external:
        return part.relate_to(partname, reltype, is_external=True)
    new = Part(PackURI(partname), content_type, part.package, blob)
    return part.relate_to(new, reltype)


def _image_rid(slide_or_part, blob):
    part = getattr(slide_or_part, "part", slide_or_part)
    _, rid = part.get_or_add_image_part(io.BytesIO(blob))
    return rid


def _next_id(sp_tree):
    ids = [int(v) for v in sp_tree.xpath(".//p:cNvPr/@id") if str(v).isdigit()]
    return max(ids + [1]) + 1


def _xfrm(x, y, w, h, tag="a:xfrm"):
    return (f'<{tag}><a:off x="{int(Inches(x))}" y="{int(Inches(y))}"/>'
            f'<a:ext cx="{int(Inches(w))}" cy="{int(Inches(h))}"/></{tag}>')


def _pic_xml(shape_id, name, rid, x, y, w, h, descr="", extra_blip="", src_rect=""):
    descr_attr = f' descr="{descr}"' if descr else ""
    return (
        f'<p:pic {NSDECL}><p:nvPicPr><p:cNvPr id="{shape_id}" name="{name}"{descr_attr}/>'
        f'<p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{rid}">{extra_blip}</a:blip>{src_rect}<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        f'<p:spPr>{_xfrm(x, y, w, h)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>'
    )


def _titled(prs, title, body=None):
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = title
    if body:
        s.shapes.add_textbox(Inches(0.5), Inches(5.5), Inches(9), Inches(1)).text_frame.text = body
    return s


def _add_video(slide, x=1, y=1.8, w=4, h=3):
    return slide.shapes.add_movie(io.BytesIO(b"\x00\x00\x00\x18ftypmp42" + b"\x01" * 256), Inches(x), Inches(y),
                                  Inches(w), Inches(h), poster_frame_image=io.BytesIO(png_bytes(160, 120, (20, 20, 90))),
                                  mime_type="video/mp4")


def build_locked_deck(path):
    """One slide per locked object type, each with a title and some body text."""
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Locked objects"
    s.placeholders[1].text = "One of each"

    # 2. video (python-pptx also writes a p:timing entry that targets it)
    s = _titled(prs, "Product demo video", "Watch the two-minute demo.")
    _add_video(s)

    # 3. audio: a movie turned into audio (a:audioFile + audio relationship)
    s = _titled(prs, "Customer voice note")
    pic = s.shapes.add_movie(io.BytesIO(b"ID3" + b"\x02" * 256), Inches(1), Inches(1.8), Inches(1), Inches(1),
                             poster_frame_image=io.BytesIO(png_bytes(64, 64, (90, 20, 20))), mime_type="audio/mpeg")
    video_file = pic._element.xpath(".//a:videoFile")[0]
    old = video_file.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}link")
    media_part = s.part.related_part(old)
    new = s.part.relate_to(media_part, RT.AUDIO)
    audio = parse_xml(f'<a:audioFile {NSDECL} r:link="{new}"/>')
    video_file.addprevious(audio)
    video_file.getparent().remove(video_file)
    s.part._rels.pop(old)

    # 4. embedded Excel object
    s = _titled(prs, "Budget workbook", "Double-click the sheet to edit the numbers.")
    s.shapes.add_ole_object(io.BytesIO(b"PK\x03\x04" + b"budget-xlsx" * 20), PROG_ID.XLSX,
                            Inches(1), Inches(1.8), Inches(4), Inches(3))

    # 5. linked Excel object (external file)
    s = _titled(prs, "Linked sales data")
    link_rid = _add_part(s, "file:///C:/Data/Sales%202026.xlsx", None, None, RT.OLE_OBJECT, external=True)
    img = _image_rid(s, png_bytes(200, 120, (30, 120, 60)))
    sp_tree = s.shapes._spTree
    sid = _next_id(sp_tree)
    sp_tree.append(parse_xml(
        f'<p:graphicFrame {NSDECL}><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="Object {sid}"/>'
        f'<p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>'
        f'{_xfrm(1, 1.8, 4, 2.4, "p:xfrm")}<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole">'
        f'<p:oleObj name="Worksheet" r:id="{link_rid}" imgW="2000000" imgH="1200000" progId="Excel.Sheet.12">'
        f'<p:link updateAutomatic="1"/>{_pic_xml(0, "", img, 1, 1.8, 4, 2.4)}</p:oleObj></a:graphicData></a:graphic></p:graphicFrame>'
    ))

    # 6. equation (PowerPoint wraps it in mc:AlternateContent with an image fallback)
    s = _titled(prs, "The growth formula", "Where r is the monthly growth rate.")
    img = _image_rid(s, png_bytes(300, 80, (255, 255, 255)))
    sp_tree = s.shapes._spTree
    sid = _next_id(sp_tree)
    math = ('<a14:m xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main">'
            '<m:oMathPara xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"><m:oMath>'
            '<m:r><m:t>R=P</m:t></m:r><m:sSup><m:e><m:r><m:t>(1+r)</m:t></m:r></m:e><m:sup><m:r><m:t>n</m:t></m:r></m:sup></m:sSup>'
            '</m:oMath></m:oMathPara></a14:m>')
    sp_tree.append(parse_xml(
        f'<mc:AlternateContent {NSDECL}><mc:Choice xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main" Requires="a14">'
        f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="TextBox {sid}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr>{_xfrm(1, 2.5, 5, 1)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></p:spPr>'
        f'<p:txBody><a:bodyPr wrap="square"><a:spAutoFit/></a:bodyPr><a:lstStyle/><a:p>{math}'
        f'<a:endParaRPr lang="en-US" sz="2800"/></a:p></p:txBody></p:sp></mc:Choice>'
        f'<mc:Fallback><p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="TextBox {sid}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr>{_xfrm(1, 2.5, 5, 1)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        f'<a:blipFill><a:blip r:embed="{img}"/><a:stretch><a:fillRect/></a:stretch></a:blipFill></p:spPr>'
        f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr lang="en-US"><a:noFill/></a:rPr><a:t> </a:t></a:r></a:p></p:txBody>'
        f'</p:sp></mc:Fallback></mc:AlternateContent>'
    ))

    # 7. 3D model with its fallback picture
    s = _titled(prs, "Warehouse layout in 3D")
    model_rid = _add_part(s, "/ppt/media/model3d1.glb", "model/gltf-binary", b"glTF\x02\x00\x00\x00" + b"\x03" * 128,
                          MODEL3D_RT)
    img = _image_rid(s, png_bytes(300, 300, (120, 120, 140)))
    sp_tree = s.shapes._spTree
    sid = _next_id(sp_tree)
    sp_tree.append(parse_xml(
        f'<mc:AlternateContent {NSDECL}><mc:Choice xmlns:am3d="http://schemas.microsoft.com/office/drawing/2017/model3d" Requires="am3d">'
        f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="3D Model {sid}"/><p:cNvGraphicFramePr>'
        f'<a:graphicFrameLocks noGrp="1" noChangeAspect="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>'
        f'{_xfrm(1, 1.8, 3, 3, "p:xfrm")}<a:graphic><a:graphicData uri="http://schemas.microsoft.com/office/drawing/2017/model3d">'
        f'<am3d:model3d r:embed="{model_rid}"><am3d:spPr>{_xfrm(1, 1.8, 3, 3)}<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></am3d:spPr>'
        f'<am3d:camera><am3d:pos x="0" y="0" z="60000000"/><am3d:up dx="0" dy="36000000" dz="0"/><am3d:lookAt x="0" y="0" z="0"/>'
        f'<am3d:perspective fov="2700000"/></am3d:camera><am3d:trans><am3d:meterPerModelUnit n="1" d="1"/>'
        f'<am3d:preTrans dx="0" dy="0" dz="0"/><am3d:scale><am3d:sx n="1" d="1"/><am3d:sy n="1" d="1"/><am3d:sz n="1" d="1"/></am3d:scale>'
        f'<am3d:rot/><am3d:postTrans dx="0" dy="0" dz="0"/></am3d:trans>'
        f'<am3d:raster rName="Office3DRenderer" rVer="16.0.8326"><am3d:blip r:embed="{img}"/></am3d:raster>'
        f'</am3d:model3d></a:graphicData></a:graphic></p:graphicFrame></mc:Choice>'
        f'<mc:Fallback>{_pic_xml(sid, f"3D Model {sid}", img, 1, 1.8, 3, 3)}</mc:Fallback></mc:AlternateContent>'
    ))

    # 8. SmartArt: graphic frame + data, layout, quick style, and colour parts
    s = _titled(prs, "Our process")
    ns = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
    dm = _add_part(s, "/ppt/diagrams/data1.xml", "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
                   SMARTART_DATA, RT.DIAGRAM_DATA)
    lo = _add_part(s, "/ppt/diagrams/layout1.xml",
                   "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml",
                   f'<dgm:layoutDef xmlns:dgm="{ns}" uniqueId="urn:microsoft.com/office/officeart/2005/8/layout/process1">'
                   '<dgm:layoutNode name="diagram"/></dgm:layoutDef>'.encode(), RT.DIAGRAM_LAYOUT)
    qs = _add_part(s, "/ppt/diagrams/quickStyle1.xml",
                   "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml",
                   f'<dgm:styleDef xmlns:dgm="{ns}" uniqueId="urn:microsoft.com/office/officeart/2005/8/quickstyle/simple1"/>'.encode(),
                   RT.DIAGRAM_QUICK_STYLE)
    cs = _add_part(s, "/ppt/diagrams/colors1.xml",
                   "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml",
                   f'<dgm:colorsDef xmlns:dgm="{ns}" uniqueId="urn:microsoft.com/office/officeart/2005/8/colors/accent1_2"/>'.encode(),
                   RT.DIAGRAM_COLORS)
    sp_tree = s.shapes._spTree
    sid = _next_id(sp_tree)
    sp_tree.append(parse_xml(
        f'<p:graphicFrame {NSDECL}><p:nvGraphicFramePr><p:cNvPr id="{sid}" name="Diagram {sid}"/><p:cNvGraphicFramePr/>'
        f'<p:nvPr/></p:nvGraphicFramePr>{_xfrm(1, 1.8, 8, 3, "p:xfrm")}<a:graphic>'
        f'<a:graphicData uri="{ns}"><dgm:relIds xmlns:dgm="{ns}" r:dm="{dm}" r:lo="{lo}" r:qs="{qs}" r:cs="{cs}"/>'
        f'</a:graphicData></a:graphic></p:graphicFrame>'
    ))

    # 9. ink (a content part) with a picture fallback
    s = _titled(prs, "Whiteboard sketch")
    ink = _add_part(s, "/ppt/ink/ink1.xml", "application/inkml+xml",
                    b'<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML"><inkml:trace>0 0, 10 10, 20 5</inkml:trace></inkml:ink>',
                    RT.CUSTOM_XML)
    img = _image_rid(s, png_bytes(200, 100, (250, 250, 250)))
    sp_tree = s.shapes._spTree
    sid = _next_id(sp_tree)
    sp_tree.append(parse_xml(
        f'<mc:AlternateContent {NSDECL}><mc:Choice xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main" Requires="p14">'
        f'<p:contentPart p14:bwMode="auto" r:id="{ink}"><p14:nvContentPartPr><p14:cNvPr id="{sid}" name="Ink {sid}"/>'
        f'<p14:cNvContentPartPr/><p14:nvPr/></p14:nvContentPartPr>{_xfrm(2, 2, 4, 2, "p14:xfrm")}</p:contentPart></mc:Choice>'
        f'<mc:Fallback>{_pic_xml(sid, f"Ink {sid}", img, 2, 2, 4, 2)}</mc:Fallback></mc:AlternateContent>'
    ))

    # 10. a group holding a video and a caption: the whole group is locked
    s = _titled(prs, "Store opening")
    grp = s.shapes.add_group_shape()
    grp.shapes.add_textbox(Inches(1), Inches(5), Inches(4), Inches(0.6)).text_frame.text = "Ribbon cutting, Kisumu"
    vid = _add_video(s, 1, 1.8, 4, 3)
    grp._element.append(vid._element)  # move the video into the group
    prs.save(path)
    return path


def build_images_deck(path):
    """Logo on the master, an image on a layout, a cropped photo, a picture background, SVG icons."""
    prs = Presentation()
    master = prs.slide_master
    rid = _image_rid(master.part, png_bytes(120, 60, (200, 30, 30)))
    tree = master.shapes._spTree
    tree.append(parse_xml(_pic_xml(_next_id(tree), "Company logo", rid, 8.6, 0.2, 1.2, 0.6, descr="Acme logo")))
    layout = prs.slide_layouts[1]
    rid = _image_rid(layout.part, png_bytes(40, 40, (30, 30, 200)))
    tree = layout.shapes._spTree
    tree.append(parse_xml(_pic_xml(_next_id(tree), "Layout badge", rid, 0.2, 6.9, 0.5, 0.5)))

    s = prs.slides.add_slide(prs.slide_layouts[0])
    s.shapes.title.text = "Acme Ltd annual review"
    s.placeholders[1].text = "Board meeting"

    # cropped photo with alt text and a title
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Our Kisumu warehouse"
    pic = s.shapes.add_picture(io.BytesIO(photo_bytes(seed=2)), Inches(0.5), Inches(1.6), Inches(5))
    pic.crop_left, pic.crop_right, pic.crop_top = 0.15, 0.05, 0.1
    cnv = pic._element.nvPicPr.cNvPr
    cnv.set("descr", "Warehouse exterior at dawn")
    cnv.set("title", "Warehouse")
    s.shapes.add_textbox(Inches(6), Inches(2), Inches(3.5), Inches(2)).text_frame.text = "Opened in 2025 with 800 tonnes of cold storage."

    # picture background
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "Where we operate"
    bg_rid = _image_rid(s, photo_bytes(800, 600, seed=1))
    c_sld = s._element.cSld
    c_sld.insert(0, parse_xml(
        f'<p:bg {NSDECL}><p:bgPr><a:blipFill dpi="0" rotWithShape="1"><a:blip r:embed="{bg_rid}"/><a:srcRect/>'
        f'<a:stretch><a:fillRect/></a:stretch></a:blipFill><a:effectLst/></p:bgPr></p:bg>'
    ))
    s.shapes.add_textbox(Inches(1), Inches(3), Inches(6), Inches(1)).text_frame.text = "Nairobi, Mombasa, Kisumu and Eldoret"

    # SVG icons with PNG fallbacks
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = "What we offer"
    tree = s.shapes._spTree
    for i, label in enumerate(["Cold storage", "Delivery", "Farmer payments"]):
        png = _image_rid(s, png_bytes(64, 64, (10 + i * 60, 60, 90)))
        svg = _add_part(s, f"/ppt/media/icon{i + 1}.svg", "image/svg+xml", SVG_ICON.replace(b"0B1F3A", f"0B1F{i}A".encode()),
                        RT.IMAGE)
        ext = (f'<a:extLst><a:ext uri="{SVG_EXT_URI}"><asvg:svgBlip '
               f'xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main" r:embed="{svg}"/></a:ext></a:extLst>')
        tree.append(parse_xml(_pic_xml(_next_id(tree), f"Icon {i + 1}", png, 1 + i * 3, 2, 1, 1, descr=f"{label} icon",
                                       extra_blip=ext)))
        s.shapes.add_textbox(Inches(0.5 + i * 3), Inches(3.2), Inches(2.5), Inches(0.6)).text_frame.text = label
    prs.save(path)
    return path
