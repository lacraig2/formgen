"""Filling a PowerPoint form.

A ``.pptx`` keeps its text on slides as DrawingML (``a:p`` / ``a:r`` / ``a:t``)
rather than in a body of ``w:p``. These build the smallest structurally valid
presentation that still exercises the real engine -- inspect and fill go
through :mod:`formgen.bench`, exactly as the browser does -- and assert that
the marker grammar behaves the same on a slide as in a document.
"""

from __future__ import annotations

import io
import zipfile

from formgen import bench
from formgen.content.presentation import is_presentation
from formgen.learn.formfields import find_repeats
from formgen.opc.package import OpcPackage

DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS = f'xmlns:a="{A}" xmlns:r="{R}" xmlns:p="{P}"'

# A 1x1 PNG -- enough for sniff() to accept and embed.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082")


def _run(text: str) -> str:
    return f'<a:r><a:rPr lang="en-US"/><a:t>{text}</a:t></a:r>'


def _para(*runs: str) -> str:
    return f'<a:p>{"".join(runs)}</a:p>'


def _shape(paragraphs: str, sid: int = 2) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sid}" name="TextBox {sid}"/>'
            f'<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/>'
            f'<p:txBody><a:bodyPr/><a:lstStyle/>{paragraphs}</p:txBody></p:sp>')


def _picture(alt: str, sid: int = 3, rid: str = "rId1") -> str:
    return (f'<p:pic><p:nvPicPr><p:cNvPr id="{sid}" name="Picture" '
            f'descr="{alt}"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
            f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/>'
            f'</a:stretch></p:blipFill><p:spPr/></p:pic>')


def _slide(shapes: str) -> bytes:
    return (
        f'{DECL}<p:sld {NS}><p:cSld><p:spTree>'
        f'<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/>'
        f'</p:nvGrpSpPr><p:grpSpPr/>{shapes}</p:spTree></p:cSld></p:sld>'
    ).encode()


def make_pptx(slides: list[str], media: dict[str, bytes] | None = None,
              slide_rels: dict[int, str] | None = None) -> bytes:
    """Bytes of a minimal presentation with one shape tree per slide.

    `media` maps a media part name to its bytes; `slide_rels` maps a slide
    number to a rels body, for the picture-swap test that needs a blip to
    point at an existing image."""
    media = media or {}
    slide_rels = slide_rels or {}
    ct_overrides = (
        '<Override PartName="/ppt/presentation.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.'
        'presentationml.presentation.main+xml"/>'
        + "".join(
            f'<Override PartName="/ppt/slides/slide{i}.xml" '
            f'ContentType="application/vnd.openxmlformats-officedocument.'
            f'presentationml.slide+xml"/>'
            for i in range(1, len(slides) + 1)))
    sld_ids = "".join(
        f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>'
        for i in range(len(slides)))
    pres_rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="{R}/slide" '
        f'Target="slides/slide{i + 1}.xml"/>'
        for i in range(len(slides)))

    blobs = {
        "[Content_Types].xml": (
            f'{DECL}<Types xmlns="{CT}">'
            '<Default Extension="rels" ContentType="application/vnd.'
            'openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            f'{ct_overrides}</Types>').encode(),
        "_rels/.rels": (
            f'{DECL}<Relationships xmlns="{PKG}"><Relationship Id="rId1" '
            f'Type="{R}/officeDocument" Target="ppt/presentation.xml"/>'
            '</Relationships>').encode(),
        "ppt/presentation.xml": (
            f'{DECL}<p:presentation {NS}><p:sldIdLst>{sld_ids}'
            '</p:sldIdLst></p:presentation>').encode(),
        "ppt/_rels/presentation.xml.rels": (
            f'{DECL}<Relationships xmlns="{PKG}">{pres_rels}'
            '</Relationships>').encode(),
    }
    for i, shapes in enumerate(slides, 1):
        blobs[f"ppt/slides/slide{i}.xml"] = _slide(shapes)
    for number, body in slide_rels.items():
        blobs[f"ppt/slides/_rels/slide{number}.xml.rels"] = (
            f'{DECL}<Relationships xmlns="{PKG}">{body}</Relationships>').encode()
    blobs.update(media)
    return bench._save(OpcPackage(blobs))


def _slide_text(data: bytes, number: int = 1) -> str:
    xml = zipfile.ZipFile(io.BytesIO(data)).read(
        f"ppt/slides/slide{number}.xml").decode()
    import re
    # Runs concatenate with no separator within the slide; joining with "" is
    # how the reader sees them (paragraph boundaries do not matter to these
    # substring assertions).
    return "".join(re.findall(r"<a:t[^>]*>(.*?)</a:t>", xml, re.S))


# -- it is recognised as a presentation ----------------------------------


def test_a_presentation_is_recognised_and_has_no_repeating_rows():
    data = make_pptx([_shape(_para(_run("{{title}}")))])
    pkg = bench._open(data)
    assert is_presentation(pkg) is True
    assert find_repeats(pkg) == []


# -- discovery ------------------------------------------------------------


def test_inspect_finds_every_marker_kind_on_a_slide():
    data = make_pptx([_shape(
        _para(_run("Prepared by {{full_name*}}"))
        + _para(_run("Status {{choice: status | Draft, Final}}"))
        + _para(_run("Signed {{check: signed}} on {{date: when}}"))
        + _para(_run("Total {{number: total}}")))])
    info = bench.inspect(data)
    assert info["kind"] == "pptx"
    by_name = {f["name"]: f for f in info["fields"]}
    assert by_name["full_name"]["kind"] == "text"
    assert by_name["full_name"]["required"] is True
    assert by_name["status"]["kind"] == "choice"
    assert by_name["status"]["choices"] == ["Draft", "Final"]
    assert by_name["signed"]["kind"] == "checkbox"
    assert by_name["when"]["kind"] == "date"
    assert by_name["total"]["kind"] == "number"


def test_a_marker_repeated_across_slides_is_one_field():
    footer = _shape(_para(_run("{{company}}")))
    data = make_pptx([footer, footer, footer])
    names = [f["name"] for f in bench.inspect(data)["fields"]]
    assert names == ["company"]


# -- fill -----------------------------------------------------------------


def test_fill_substitutes_text_choice_and_check():
    data = make_pptx([_shape(
        _para(_run("Prepared by {{full_name}} ({{choice: status | Draft, Final}})"))
        + _para(_run("Signed {{check: signed}}")))])
    out, report = bench.fill_document(
        data, text={"full_name": "K. Ito", "status": "Final"},
        checks={"signed": True})
    assert report["kind"] == "pptx"
    text = _slide_text(out)
    assert "Prepared by K. Ito (Final)" in text
    assert "☒" in text                       # ticked ballot box
    assert set(report["filled"]) == {"full_name", "status", "signed"}
    assert report["leftover"] == []


def test_a_marker_split_across_runs_is_pulled_together_and_filled():
    data = make_pptx([_shape(_para(
        _run("Ref {{re"), _run("f}} end")))])
    out, report = bench.fill_document(data, text={"ref": "ABC-1"})
    assert "Ref ABC-1 end" in _slide_text(out)
    assert report["filled"] == ["ref"]


def test_a_multiline_value_becomes_a_break_on_the_slide():
    data = make_pptx([_shape(_para(_run("{{addr}}")))])
    out, _ = bench.fill_document(data, text={"addr": "Line one\nLine two"})
    xml = zipfile.ZipFile(io.BytesIO(out)).read("ppt/slides/slide1.xml").decode()
    assert "<a:br" in xml
    assert "Line one" in xml and "Line two" in xml


def test_an_unfilled_marker_is_reported_as_leftover():
    data = make_pptx([_shape(_para(_run("Hi {{name}} and {{other}}")))])
    _, report = bench.fill_document(data, text={"name": "A"})
    assert report["leftover"] == ["{{other}}"]


def test_a_slide_with_no_markers_is_left_byte_for_byte():
    plain = _shape(_para(_run("nothing to fill here")))
    marked = _shape(_para(_run("{{x}}")))
    data = make_pptx([marked, plain])
    before = zipfile.ZipFile(io.BytesIO(data)).read("ppt/slides/slide2.xml")
    out, _ = bench.fill_document(data, text={"x": "y"})
    after = zipfile.ZipFile(io.BytesIO(out)).read("ppt/slides/slide2.xml")
    assert before == after


# -- pictures marked in alt text -----------------------------------------


def test_a_picture_marked_in_its_alt_text_has_its_image_swapped():
    data = make_pptx(
        [_shape(_para(_run("caption"))) + _picture("{{hero | Product}}")],
        media={"ppt/media/image1.png": b"OLD-IMAGE-BYTES"},
        slide_rels={1: f'<Relationship Id="rId1" Type="{R}/image" '
                       'Target="../media/image1.png"/>'})
    info = bench.inspect(data)
    hero = next(f for f in info["fields"] if f["name"] == "hero")
    assert hero["kind"] == "image" and hero["label"] == "Product"

    out, report = bench.fill_document(data, images={"hero": PNG})
    assert report["filled"] == ["hero"]
    xml = zipfile.ZipFile(io.BytesIO(out)).read("ppt/slides/slide1.xml").decode()
    assert 'descr="Product"' in xml           # marker rewritten to the label
    assert "{{hero" not in xml
    # the swapped-in PNG is present as a media part, and the blip now points at
    # it (a relationship the slide's rels resolve).
    zf = zipfile.ZipFile(io.BytesIO(out))
    media = {n: zf.read(n) for n in zf.namelist()
             if "/media/" in n and n.endswith(".png")}
    assert PNG in media.values()              # the new image really landed
    assert b"OLD" not in b"".join(media.values())   # the orphan was released


def test_a_marked_picture_with_no_image_supplied_is_left_alone():
    data = make_pptx(
        [_picture("{{hero}}")],
        media={"ppt/media/image1.png": b"OLD"},
        slide_rels={1: f'<Relationship Id="rId1" Type="{R}/image" '
                       'Target="../media/image1.png"/>'})
    out, report = bench.fill_document(data, text={})
    assert "hero" in report["untouched"]
    same = zipfile.ZipFile(io.BytesIO(out)).read("ppt/slides/slide1.xml")
    assert b"{{hero}}" in same                # untouched, still marked


# -- self-refill ----------------------------------------------------------


def test_a_filled_presentation_remembers_its_answers():
    data = make_pptx([_shape(_para(_run("Name {{full_name}}")))])
    filled, _ = bench.fill_document(data, text={"full_name": "K. Ito"})
    again = bench.inspect(filled)
    assert again["kind"] == "pptx"
    # the embedded blank template still shows the field, prefilled
    assert any(f["name"] == "full_name" for f in again["fields"])
    assert again["prefill"]["text"]["full_name"] == "K. Ito"
