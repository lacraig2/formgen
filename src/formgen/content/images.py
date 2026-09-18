"""Putting an image into a package: the part, the relationship, the drawing.

`emit` grows a document from Markdown and drops images into fresh body runs;
`fill` keeps a template whole and swaps the image inside a picture content
control. Both need the same three things -- a media part, an `r:id` on the
document that points at it, and (sometimes) the `w:drawing` XML that displays
it -- so those live here rather than being written twice and drifting.

Sizes are in EMU (914400 to the inch), the unit `wp:extent` and `a:ext` want.
"""

from __future__ import annotations

import posixpath

from lxml import etree

from ..opc.ns import NS, RT, qn
from ..opc.package import OpcPackage
from .imageinfo import read_image_bytes

EMU_PER_INCH = 914400
DEFAULT_DPI = 96


def add_image_part(pkg: OpcPackage, data: bytes, extension: str,
                   content_type: str, owner: str = "",
                   media_dir: str = "") -> str:
    """Add `data` as a media part and relate `owner` to it; return the id.

    The part is minted in a `media/` folder, its content type registered, and
    an `r:id` handed back for a blip's `r:embed`. By default the folder sits
    beside the owning part (`word/media/imageN.ext`); `media_dir` overrides
    that when the owner and the media convention part ways -- a slide relates
    to its image, but PowerPoint keeps every deck's media in `ppt/media/`, not
    under `ppt/slides/`.
    """
    owner = owner or pkg.main_document
    folder = media_dir or posixpath.dirname(owner)
    partname = pkg.unique_partname(
        posixpath.join(folder, f"media/image{{n}}{extension}")
    )
    pkg.add_part(partname, data, content_type)
    return pkg.relate(RT["image"], partname, owner)


def native_emu(data: bytes) -> tuple[int, int] | None:
    """An image's intrinsic size in EMU, or None if it can't be read.

    DPI decides how many pixels make an inch; a file that records none is
    assumed to be 96, the same default `emit` uses.
    """
    info = read_image_bytes(data)
    if info is None:
        return None
    dpi = info.dpi or (DEFAULT_DPI, DEFAULT_DPI)
    # A malformed file can report zero or a negative resolution; fall back
    # rather than divide by it and get a 1-EMU (invisible) or negative image.
    x_dpi = float(dpi[0]) if float(dpi[0]) > 0 else DEFAULT_DPI
    y_dpi = float(dpi[1]) if float(dpi[1]) > 0 else DEFAULT_DPI
    width = int(info.size[0] / x_dpi * EMU_PER_INCH)
    height = int(info.size[1] / y_dpi * EMU_PER_INCH)
    return max(width, 1), max(height, 1)


def fit_within(native: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """Scale `native` to the largest size that fits `box`, keeping aspect.

    The template author drew the slot; an uploaded photo should sit inside it
    at its own proportions rather than stretch to fill it and distort.
    """
    nw, nh = native
    bw, bh = box
    if nw <= 0 or nh <= 0 or bw <= 0 or bh <= 0:
        return box
    scale = min(bw / nw, bh / nh)
    return max(int(nw * scale), 1), max(int(nh * scale), 1)


def build_picture_run(rid: str, width: int, height: int, drawing_id: int,
                      name: str, alt: str = "") -> etree._Element:
    """A `w:r` holding an inline `w:drawing` for the image behind `rid`.

    Used when a picture content control is empty -- it has no blip to swap, so
    fill builds the drawing from scratch, the same shape `emit` writes.
    """
    run = etree.Element(qn("w:r"))
    drawing = etree.SubElement(run, qn("w:drawing"))
    inline = etree.SubElement(drawing, qn("wp:inline"))
    for edge in ("distT", "distB", "distL", "distR"):
        inline.set(edge, "0")
    extent = etree.SubElement(inline, qn("wp:extent"))
    extent.set("cx", str(width))
    extent.set("cy", str(height))
    effect = etree.SubElement(inline, qn("wp:effectExtent"))
    for edge in ("l", "t", "r", "b"):
        effect.set(edge, "0")
    doc_pr = etree.SubElement(inline, qn("wp:docPr"))
    doc_pr.set("id", str(drawing_id))
    doc_pr.set("name", name)
    if alt:
        doc_pr.set("descr", alt)
    frame = etree.SubElement(inline, qn("wp:cNvGraphicFramePr"))
    etree.SubElement(frame, qn("a:graphicFrameLocks")).set("noChangeAspect", "1")
    graphic = etree.SubElement(inline, qn("a:graphic"))
    graphic_data = etree.SubElement(graphic, qn("a:graphicData"))
    graphic_data.set("uri", NS["pic"])
    pic = etree.SubElement(graphic_data, qn("pic:pic"))
    nv = etree.SubElement(pic, qn("pic:nvPicPr"))
    c_nv = etree.SubElement(nv, qn("pic:cNvPr"))
    c_nv.set("id", "0")
    c_nv.set("name", name)
    if alt:
        c_nv.set("descr", alt)
    etree.SubElement(nv, qn("pic:cNvPicPr"))
    blip_fill = etree.SubElement(pic, qn("pic:blipFill"))
    etree.SubElement(blip_fill, qn("a:blip")).set(qn("r:embed"), rid)
    stretch = etree.SubElement(blip_fill, qn("a:stretch"))
    etree.SubElement(stretch, qn("a:fillRect"))
    sp = etree.SubElement(pic, qn("pic:spPr"))
    xfrm = etree.SubElement(sp, qn("a:xfrm"))
    offset = etree.SubElement(xfrm, qn("a:off"))
    offset.set("x", "0")
    offset.set("y", "0")
    ext = etree.SubElement(xfrm, qn("a:ext"))
    ext.set("cx", str(width))
    ext.set("cy", str(height))
    geometry = etree.SubElement(sp, qn("a:prstGeom"))
    geometry.set("prst", "rect")
    etree.SubElement(geometry, qn("a:avLst"))
    return run
