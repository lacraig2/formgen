"""Marking a picture that is already in the document as an image field.

An author drops a placeholder picture into Word, sizes and positions it, and
writes a marker in its Alt Text (right-click > Edit Alt Text). formgen finds it
as an image field and, at fill time, swaps the image *inside* the picture --
the drawing, its extent, its position and its wrap are the author's and are left
untouched. This is the way to give a picture a real home in the layout, which a
text ``{{image: ...}}`` marker cannot.
"""

from __future__ import annotations

import base64

from lxml import etree

from fixtures import build
from formgen import bench
from formgen.content.images import add_image_part, build_picture_run
from formgen.learn.formfields import IMAGE, find_fields
from formgen.opc.ns import qn
from formgen.safety.verify import check_integrity

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMCAQAY0X2gAAAAAElFTkSuQmCC"
)
EXTENT = (1828800, 914400)   # the author's 2in x 1in


def marked_picture(alt, extent=EXTENT) -> bytes:
    """A document with one inline picture whose alt text is `alt`."""
    pkg = build.make(body=build.para("Logo:", style="BodyText"))
    rid = add_image_part(pkg, PNG, ".png", "image/png")
    run = build_picture_run(rid, extent[0], extent[1], 7, "Logo")
    if alt is not None:
        for doc_pr in run.iter(qn("wp:docPr")):
            doc_pr.set("descr", alt)
        for cnv in run.iter(qn("pic:cNvPr")):
            cnv.set("descr", alt)
    root = pkg.edit(pkg.main_document)
    body = root.find(qn("w:body"))
    para = etree.SubElement(body, qn("w:p"))
    para.append(run)
    sect = body.find(qn("w:sectPr"))            # sectPr must stay last
    if sect is not None:
        body.remove(para)
        body.insert(list(body).index(sect), para)
    pkg.touch(pkg.main_document)
    return bench._save(pkg)


def _drawing(data):
    root = bench._open(data).element(bench._open(data).main_document)
    return (root.find(".//" + qn("a:blip")),
            root.find(".//" + qn("wp:extent")),
            root.find(".//" + qn("wp:docPr")))


# -- discovery ------------------------------------------------------------

def test_a_picture_marked_in_alt_text_is_an_image_field():
    fields = find_fields(bench._open(marked_picture("{{image: logo}}"))).fields
    assert [(f.name, f.kind, f.source) for f in fields] == [("logo", IMAGE, "picture")]


def test_a_plain_picture_is_not_a_field():
    assert find_fields(bench._open(marked_picture("A company logo"))).fields == []
    assert find_fields(bench._open(marked_picture(None))).fields == []


def test_a_bare_name_marker_still_reads_as_an_image():
    # It is a picture, so any marker means "swap this image", prefix or not.
    fields = find_fields(bench._open(marked_picture("{{headshot}}"))).fields
    assert [(f.name, f.kind) for f in fields] == [("headshot", IMAGE)]


# -- filling --------------------------------------------------------------

def test_fill_swaps_the_image_but_keeps_the_frame():
    doc = marked_picture("{{image: logo}}")
    blip0, ext0, _ = _drawing(doc)
    filled, info = bench.fill_document(doc, images={"logo": PNG})
    blip1, ext1, _ = _drawing(filled)
    assert blip1.get(qn("r:embed")) != blip0.get(qn("r:embed"))     # new image
    assert (ext1.get("cx"), ext1.get("cy")) == (ext0.get("cx"), ext0.get("cy"))  # frame kept
    assert info["filled"] == ["logo"]
    assert info["leftover"] == []
    assert check_integrity(bench._open(filled)) == []


def test_the_marker_leaves_the_alt_text_as_the_label():
    doc = marked_picture("{{image: logo | Company logo}}")
    assert find_fields(bench._open(doc)).fields[0].label == "Company logo"
    filled, _ = bench.fill_document(doc, images={"logo": PNG})
    _, _, doc_pr = _drawing(filled)
    assert doc_pr.get("descr") == "Company logo"     # readable alt, no {{...}}


def test_a_required_marked_picture_is_reported_when_unfilled():
    doc = marked_picture("{{image: logo*}}")
    assert find_fields(bench._open(doc)).fields[0].required is True
    _, info = bench.fill_document(doc, images={})
    assert "logo" in info["untouched"]               # left as-is, nothing supplied
