"""Filling a picture content control with an uploaded image.

A picture content control is how Word marks an image slot -- Developer tab,
Picture content control, tag it. formgen discovers it as an `image` field and
`fill` puts a real image into it: the media part, the relationship, the blip,
and an extent that fits the image into the box the template author drew,
without ever rebuilding a run or disturbing the surrounding text.
"""

from __future__ import annotations

import io

import pytest

from fixtures import build
from formgen.content.fill import fill
from formgen.learn.formfields import IMAGE, find_fields
from formgen.opc.ns import qn
from formgen.opc.package import OpcPackage
from formgen.safety.verify import check_integrity

PILImage = pytest.importorskip("PIL.Image")


def png(size=(800, 600), color="white") -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def picture_control(name="headshot", extent="1905000",
                    placeholder="Click to add a picture") -> str:
    """An inline picture content control in its placeholder state -- the shape
    Word writes for an empty <Picture> control, tag and all."""
    return (
        "<w:sdt><w:sdtPr>"
        f'<w:alias w:val="{name.title()}"/>'
        f'<w:tag w:val="formgen.{name}"/>'
        '<w:id w:val="42"/><w:picture/><w:showingPlcHdr/>'
        "</w:sdtPr><w:sdtContent>"
        f'<w:r><w:t xml:space="preserve">{placeholder}</w:t></w:r>'
        "</w:sdtContent></w:sdt>"
    )


def a_template() -> OpcPackage:
    """A form: a text control, a picture control, and body text around them."""
    text_control = (
        '<w:sdt><w:sdtPr><w:alias w:val="Name"/>'
        '<w:tag w:val="formgen.full_name"/><w:id w:val="7"/><w:text/>'
        '</w:sdtPr><w:sdtContent>'
        '<w:r><w:t xml:space="preserve">Enter name</w:t></w:r>'
        '</w:sdtContent></w:sdt>'
    )
    body = (
        build.para("Personnel Record", style="Title")
        + build.para("Name: ", runs=text_control, style="BodyText")
        + build.para("Photo: ", runs=picture_control(), style="BodyText")
        + build.para("Approved for release.", style="BodyText")
    )
    return build.make(body=body)


def _blips(pkg):
    root = pkg.element(pkg.main_document)
    return list(root.iter(qn("a:blip")))


def _media_parts(pkg):
    return [n for n in pkg.names() if "media/image" in n]


def _body_text(pkg):
    root = pkg.element(pkg.main_document)
    return "".join(t.text or "" for t in root.iter(qn("w:t")))


def test_a_picture_control_is_discovered_as_an_image_field():
    fields = {f.name: f for f in find_fields(a_template()).fields}
    assert "headshot" in fields
    assert fields["headshot"].kind == IMAGE
    # The neighbouring text control is still just text.
    assert fields["full_name"].kind != IMAGE


def test_filling_a_picture_control_embeds_the_image(tmp_path):
    pkg = a_template()
    report = fill(pkg, {"headshot": png(), "full_name": "K. Ito"})

    assert "headshot" in report.filled
    assert check_integrity(pkg) == []           # every r:embed resolves
    assert len(_blips(pkg)) == 1                  # exactly one image, ours
    media = _media_parts(pkg)
    assert len(media) == 1 and pkg.blob(media[0])[:8] == b"\x89PNG\r\n\x1a\n"
    # The placeholder prompt is gone; the surrounding text is untouched.
    text = _body_text(pkg)
    assert "Click to add a picture" not in text
    assert "Personnel Record" in text and "Approved for release." in text
    assert "K. Ito" in text
    # It still saves (which re-runs the package's own structural check).
    pkg.save(tmp_path / "filled.docx", deterministic=True)


def test_a_large_image_is_capped_to_the_page_keeping_aspect():
    # An empty placeholder control carries no box, so the image lands at its
    # own size -- but a big one is shrunk to the page cap, aspect intact.
    pkg = a_template()
    fill(pkg, {"headshot": png(size=(8000, 4000))})  # 2:1, far too big
    extent = pkg.element(pkg.main_document).find(".//" + qn("wp:extent"))
    cx, cy = int(extent.get("cx")), int(extent.get("cy"))
    assert cx <= 5486400 and cy <= 6858000           # inside the page cap
    assert cy == pytest.approx(cx / 2, rel=0.02)      # aspect preserved


def test_swapping_an_image_already_in_a_control(tmp_path):
    # Fill once (builds the drawing), save, reopen, fill again (swaps the blip).
    pkg = a_template()
    fill(pkg, {"headshot": png(color="white")})
    pkg.save(tmp_path / "one.docx", deterministic=True)

    again = OpcPackage.open(tmp_path / "one.docx")
    embed_before = _blips(again)[0].get(qn("r:embed"))
    report = fill(again, {"headshot": png(size=(300, 300), color="black")})

    assert "headshot" in report.filled
    assert check_integrity(again) == []
    assert len(_blips(again)) == 1
    assert _blips(again)[0].get(qn("r:embed")) != embed_before  # repointed
    # The image it replaced must not linger in the package -- for a real
    # template that would be leaving somebody's photo in the file.
    assert len(_media_parts(again)) == 1
    again.save(tmp_path / "two.docx", deterministic=True)


def test_an_unsupplied_picture_slot_is_left_alone_not_cleared():
    # Images carry no safe "empty" value, so an unsupplied picture slot is kept
    # rather than blanked -- unlike a text control, which is cleared by default.
    pkg = a_template()
    report = fill(pkg, {})            # supply nothing
    assert "headshot" in report.untouched
    assert _media_parts(pkg) == []    # nothing embedded
    assert _blips(pkg) == []
    assert check_integrity(pkg) == []
    assert "full_name" in report.cleared   # the text control still cleared


def test_a_text_value_does_not_fill_a_picture_slot():
    # A picture slot needs image bytes; a string is not a filename, and must
    # never be written into the control as if it were a caption.
    pkg = a_template()
    report = fill(pkg, {"headshot": "not an image"})
    assert "headshot" not in report.filled
    assert _media_parts(pkg) == []
    assert "not an image" not in _body_text(pkg)
    assert check_integrity(pkg) == []


# -- the simple path: typed markers, no content controls at all -----------

def marker_template() -> OpcPackage:
    """The whole authoring convention: markers typed into an ordinary doc.

    `{{full_name}}` is a text slot; `{{image: headshot}}` is a picture slot.
    The signature marker sits mid-sentence, to prove the words around it stay.
    """
    body = (
        build.para("Personnel Record", style="Title")
        + build.para("Name: {{full_name}}", style="BodyText")
        + build.para("{{image: headshot}}", style="BodyText")
        + build.para("Signed: sig {{image: signature}} end.", style="BodyText")
        + build.para("Approved for release.", style="BodyText")
    )
    return build.make(body=body)


def test_markers_are_discovered_with_the_right_kinds():
    fields = {f.name: f for f in find_fields(marker_template()).fields}
    assert fields["full_name"].kind != IMAGE
    assert fields["headshot"].kind == IMAGE
    assert fields["signature"].kind == IMAGE


def test_filling_marker_slots_places_text_and_images(tmp_path):
    pkg = marker_template()
    report = fill(pkg, {
        "full_name": "K. Ito",
        "headshot": png(size=(600, 600)),
        "signature": png(size=(300, 100)),
    })

    assert set(report.filled) >= {"full_name", "headshot", "signature"}
    assert check_integrity(pkg) == []
    assert len(_blips(pkg)) == 2 and len(_media_parts(pkg)) == 2

    text = _body_text(pkg)
    assert "K. Ito" in text
    assert "{{" not in text and "image:" not in text   # every marker consumed
    assert "sig " in text and " end." in text           # neighbours preserved
    assert "Approved for release." in text
    pkg.save(tmp_path / "filled.docx", deterministic=True)


def test_an_unsupplied_image_marker_is_left_as_typed():
    # Nothing supplied for the picture: the marker text stays, so the author
    # sees an obviously-unfilled slot rather than a silently dropped one.
    pkg = marker_template()
    report = fill(pkg, {"full_name": "K. Ito"})
    text = _body_text(pkg)
    assert "{{image: headshot}}" in text
    assert "headshot" not in report.filled
    assert _media_parts(pkg) == []
    assert check_integrity(pkg) == []
