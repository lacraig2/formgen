"""fill produces the document you would get by typing the values in yourself.

The claim these tests make precise: filling a template's markers is not an
approximation of a filled document -- it *is* the filled document. For each
mechanism we build the same document two ways, the formgen way (markers, then
`fill`) and the by-hand way (the final values authored in place), and assert
the two packages are identical part-for-part.

"Identical" is canonical (C14N), not byte-for-byte, because lxml reserializes
a part it touches -- attribute quoting, namespace-declaration order, the
spacing inside a tag -- without changing what the XML *means*. C14N compares
the infoset Word actually reads; two documents equal under it render the same.
Untouched parts and embedded media are held to the stricter byte-for-byte bar.
"""

from __future__ import annotations

import io

import pytest

from fixtures import build
from formgen.content.fill import fill
from formgen.opc.package import OpcPackage
from lxml import etree

PILImage = pytest.importorskip("PIL.Image")


def _canon(name: str, blob: bytes) -> bytes:
    if name.endswith(".xml") or name.endswith(".rels"):
        return etree.tostring(etree.fromstring(blob), method="c14n")
    return blob


def assert_same_document(produced: OpcPackage, expected: OpcPackage,
                         *, allow_extra_media: bool = False) -> None:
    """Every part of `produced` matches `expected` canonically (media byte-wise)."""
    a = {n: produced.blob(n) for n in produced.names()}
    b = {n: expected.blob(n) for n in expected.names()}
    differing = [n for n in sorted(set(a) & set(b))
                 if _canon(n, a[n]) != _canon(n, b[n])]
    only_produced = sorted(set(a) - set(b))
    only_expected = sorted(set(b) - set(a))
    if allow_extra_media:
        only_produced = [n for n in only_produced if "media" not in n]
    assert differing == [], f"parts differ: {differing}"
    assert only_produced == [], f"parts only in produced: {only_produced}"
    assert only_expected == [], f"parts only in expected: {only_expected}"


def para(*a, **k):
    return build.para(*a, **k)


# (label, template body, values, the same document authored by hand)
CASES = [
    (
        "text",
        para("Report by {{author}} on {{date}}.", style="BodyText"),
        {"author": "K. Ito", "date": "2026-09-16"},
        para("Report by K. Ito on 2026-09-16.", style="BodyText"),
    ),
    (
        "multi-line text",
        para("Address: {{addr}}", style="BodyText"),
        {"addr": "12 Elm St\nApt 4\nBoston"},
        # One run with soft line breaks -- how Word stores Shift+Enter.
        para("", style="BodyText", runs=(
            '<w:r><w:t xml:space="preserve">Address: 12 Elm St</w:t>'
            '<w:br/><w:t xml:space="preserve">Apt 4</w:t>'
            '<w:br/><w:t xml:space="preserve">Boston</w:t></w:r>')),
    ),
    (
        "checkbox and choice",
        para("Cleared {{check: ok}}  Status {{choice: s | Draft, Final}}",
             style="BodyText"),
        {"ok": True, "s": "Final"},
        para("Cleared ☒  Status Final", style="BodyText"),
    ),
    (
        "marker inside a formatted run",
        para("Ref: ", style="BodyText", runs=(
            '<w:r><w:rPr><w:b/></w:rPr>'
            '<w:t xml:space="preserve">{{ref}}</w:t></w:r>')),
        {"ref": "LR-2026-0042"},
        para("Ref: ", style="BodyText", runs=(
            '<w:r><w:rPr><w:b/></w:rPr>'
            '<w:t xml:space="preserve">LR-2026-0042</w:t></w:r>')),
    ),
    (
        "content control",
        para("", runs=(
            '<w:sdt><w:sdtPr><w:tag w:val="formgen.name"/><w:id w:val="1"/>'
            '<w:text/><w:showingPlcHdr/></w:sdtPr><w:sdtContent>'
            '<w:r><w:t xml:space="preserve">Enter name</w:t></w:r>'
            '</w:sdtContent></w:sdt>')),
        {"name": "Ito"},
        para("", runs=(
            '<w:sdt><w:sdtPr><w:tag w:val="formgen.name"/><w:id w:val="1"/>'
            '<w:text/></w:sdtPr><w:sdtContent>'
            '<w:r><w:t xml:space="preserve">Ito</w:t></w:r>'
            '</w:sdtContent></w:sdt>')),
    ),
]


def _ff(name: str, result: str) -> str:
    return (f'<w:r><w:fldChar w:fldCharType="begin"><w:ffData>'
            f'<w:name w:val="{name}"/><w:textInput/></w:ffData></w:fldChar></w:r>'
            f'<w:r><w:instrText xml:space="preserve"> FORMTEXT </w:instrText></w:r>'
            f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            f'<w:r><w:t xml:space="preserve">{result}</w:t></w:r>'
            f'<w:r><w:fldChar w:fldCharType="end"/></w:r>')


@pytest.mark.parametrize("label,template_body,values,target_body", CASES,
                         ids=[c[0] for c in CASES])
def test_fill_equals_authoring_the_values_by_hand(label, template_body, values,
                                                  target_body):
    produced = build.make(body=template_body)
    fill(produced, values)
    expected = build.make(body=target_body)
    assert_same_document(produced, expected)


def test_legacy_form_field_result_matches_typing_it():
    produced = build.make(body=para("", runs=_ff("City", "")))
    fill(produced, {"city": "Boston"})
    expected = build.make(body=para("", runs=_ff("City", "Boston")))
    assert_same_document(produced, expected)


def test_a_repeating_table_matches_authoring_the_rows():
    template = build.table(
        build.cell(para("Item")) + build.cell(para("Qty")),
        build.cell(para("{{it.name}}")) + build.cell(para("{{it.qty}}")))
    produced = build.make(body=template)
    fill(produced, {"it": [{"name": "Bolt", "qty": "12"},
                           {"name": "Nut", "qty": "8"}]})
    authored = build.table(
        build.cell(para("Item")) + build.cell(para("Qty")),
        build.cell(para("Bolt")) + build.cell(para("12")),
        build.cell(para("Nut")) + build.cell(para("8")))
    expected = build.make(body=authored)
    assert_same_document(produced, expected)


# -- preservation: untouched parts are byte-for-byte, verbatim media ------

def test_fill_changes_only_the_document_part():
    template = build.make(body=para("Name: {{name}}", style="BodyText"))
    before = {n: template.blob(n) for n in template.names()}
    fill(template, {"name": "Ito"})
    after = {n: template.blob(n) for n in template.names()}
    changed = [n for n in before if before[n] != after.get(n)]
    # Only the main document is rewritten; styles, numbering, theme, settings,
    # content types and rels are all returned byte-for-byte.
    assert changed == ["word/document.xml"]


def _png(size=(64, 48), color="teal") -> bytes:
    buffer = io.BytesIO()
    PILImage.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_an_embedded_image_is_the_uploaded_bytes_verbatim():
    image = _png()
    pkg = build.make(body=para("Logo {{image: logo}}", style="BodyText"))
    fill(pkg, {"logo": image})
    media = [n for n in pkg.names() if "media" in n]
    assert len(media) == 1
    # Embedded, never re-encoded: the bytes in the .docx are the upload's bytes.
    assert pkg.blob(media[0]) == image


def test_filling_the_same_inputs_twice_is_byte_identical(tmp_path):
    def make_and_fill():
        pkg = build.make(body=(
            para("By {{author}}", style="BodyText")
            + para("Pic {{image: logo}}", style="BodyText")))
        fill(pkg, {"author": "Ito", "logo": _png()})
        path = tmp_path / "x.docx"
        pkg.save(path, deterministic=True)
        return path.read_bytes()

    assert make_and_fill() == make_and_fill()
