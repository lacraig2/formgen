"""The marker conventions a non-technical author can type into a document.

`{{name}}` is text, `{{check: x}}` a tick box, `{{choice: x | a, b}}` a pick
list, and a dotted `{{items.qty}}` a column of a repeating row. All of it is
just text typed into Word -- no Developer tab, no content controls -- and all
of it fills in place, so the document's own formatting is what survives.
"""

from __future__ import annotations

from fixtures import build
from formgen.content.fill import CHECKED, UNCHECKED, fill
from formgen.learn.formfields import CHECKBOX, CHOICE, find_fields, find_repeats
from formgen.opc.ns import qn
from formgen.opc.package import OpcPackage
from formgen.safety.verify import check_integrity


def text_of(pkg):
    root = pkg.element(pkg.main_document)
    return "".join(t.text or "" for t in root.iter(qn("w:t")))


def rows(pkg):
    return list(pkg.element(pkg.main_document).iter(qn("w:tr")))


def breaks(pkg):
    return list(pkg.element(pkg.main_document).iter(qn("w:br")))


# -- multi-line text ------------------------------------------------------

def test_a_multiline_value_becomes_real_line_breaks():
    pkg = build.make(body=build.para("Address: {{addr}}", style="BodyText"))
    fill(pkg, {"addr": "12 Elm St\nApt 4\nBoston, MA"})
    pieces = [t.text for t in pkg.element(pkg.main_document).iter(qn("w:t"))]
    assert len(breaks(pkg)) == 2                     # two newlines, two w:br
    assert "Apt 4" in pieces and "Boston, MA" in pieces
    assert not any("\n" in (p or "") for p in pieces)  # never a raw newline
    assert check_integrity(pkg) == []


def test_a_single_line_value_is_still_one_run(tmp_path):
    pkg = build.make(body=build.para("Name: {{name}}", style="BodyText"))
    fill(pkg, {"name": "K. Ito"})
    assert breaks(pkg) == []
    assert "Name: K. Ito" in text_of(pkg)


# -- check boxes ----------------------------------------------------------

def test_a_check_marker_is_found_and_ticked():
    pkg = build.make(body=build.para("Agree: {{check: agreed}}", style="BodyText"))
    fields = {f.name: f for f in find_fields(pkg).fields}
    assert fields["agreed"].kind == CHECKBOX
    fill(pkg, {"agreed": True})
    assert CHECKED in text_of(pkg) and UNCHECKED not in text_of(pkg)


def test_a_falsey_check_marker_shows_an_empty_box():
    pkg = build.make(body=build.para("{{check: ok}}", style="BodyText"))
    fill(pkg, {"ok": False})
    assert UNCHECKED in text_of(pkg)


# -- choices --------------------------------------------------------------

def test_a_choice_marker_carries_its_options_and_fills():
    pkg = build.make(body=build.para(
        "Status: {{choice: status | Draft, Final, Withdrawn}}", style="BodyText"))
    field = {f.name: f for f in find_fields(pkg).fields}["status"]
    assert field.kind == CHOICE
    assert field.choices == ("Draft", "Final", "Withdrawn")
    fill(pkg, {"status": "Final"})
    assert "Status: Final" in text_of(pkg)


# -- repeating rows -------------------------------------------------------

def a_table_template() -> OpcPackage:
    header = build.cell(build.para("Item")) + build.cell(build.para("Qty"))
    template = (build.cell(build.para("{{items.name}}"))
                + build.cell(build.para("{{items.qty}}")))
    body = (build.para("Bill of materials", style="Title")
            + build.table(header, template)
            + build.para("Prepared by {{author}}.", style="BodyText"))
    return build.make(body=body)


def test_a_repeating_group_is_discovered_with_its_columns():
    groups = find_repeats(a_table_template())
    assert len(groups) == 1
    assert groups[0].name == "items"
    assert [c.name for c in groups[0].columns] == ["name", "qty"]


def test_dotted_markers_are_not_also_flat_fields():
    names = [f.name for f in find_fields(a_table_template()).fields]
    assert "author" in names               # an ordinary field still shows
    assert not any("." in n or n.startswith("items") for n in names)


def test_a_row_is_cloned_once_per_record(tmp_path):
    pkg = a_table_template()
    report = fill(pkg, {
        "items": [{"name": "Bolt", "qty": "12"},
                  {"name": "Nut", "qty": "12"},
                  {"name": "Washer", "qty": "24"}],
        "author": "K. Ito",
    })
    assert "items" in report.filled and "author" in report.filled
    assert check_integrity(pkg) == []
    assert len(rows(pkg)) == 4              # header + three records
    body = text_of(pkg)
    assert all(x in body for x in ("Bolt", "Nut", "Washer"))
    assert "{{" not in body                 # every marker consumed
    assert "Prepared by K. Ito." in body
    pkg.save(tmp_path / "bom.docx", deterministic=True)


def test_an_empty_list_drops_the_template_row():
    pkg = a_table_template()
    fill(pkg, {"items": []})
    assert len(rows(pkg)) == 1              # only the header survives
    assert "{{items" not in text_of(pkg)   # the template row is gone
    assert check_integrity(pkg) == []


def test_a_paragraph_repeats_when_the_marker_is_not_in_a_table():
    pkg = build.make(body=(
        build.para("Attendees", style="Heading1")
        + build.para("- {{people.name}} ({{people.role}})", style="BodyText")))
    fill(pkg, {"people": [{"name": "Ito", "role": "chair"},
                          {"name": "Rao", "role": "scribe"}]})
    body = text_of(pkg)
    assert "Ito (chair)" in body and "Rao (scribe)" in body
    assert check_integrity(pkg) == []


def test_a_legacy_field_after_a_repeat_row_is_not_wiped():
    # Cloning the repeat row shifts the rows below it. Field discovery must see
    # that final layout, or the legacy field one row down is keyed to a stale
    # path, found nothing, and cleared -- losing the value the caller supplied.
    header = build.cell(build.para("Item"))
    repeat = build.cell(build.para("{{items.name}}"))
    total = build.cell(build.para("", runs=build.form_text("Total", result="OLD")))
    pkg = build.make(body=build.table(header, repeat, total))
    report = fill(pkg, {"items": [{"name": "A"}, {"name": "B"}], "total": "42"})
    body = text_of(pkg)
    assert "total" in report.filled and "42" in body
    assert "OLD" not in body                         # not wrongly cleared
    assert check_integrity(pkg) == []


# -- edits outside the body ----------------------------------------------

def test_a_marker_in_a_header_is_filled_and_survives_saving(tmp_path):
    # Edits to a header/footer part must be marked dirty, or save() writes the
    # part back byte-for-byte and the fill silently vanishes -- and a donor
    # value meant to be cleared ships instead.
    pkg = build.make(body=build.para("Body {{name}}", style="BodyText"))
    build.add_hdrftr(pkg, "header", "header1.xml", "Confidential -- {{name}}")
    fill(pkg, {"name": "K. Ito"})
    pkg.save(tmp_path / "doc.docx", deterministic=True)

    reopened = OpcPackage.open(tmp_path / "doc.docx")
    header = "".join(t.text or "" for t
                     in reopened.element("word/header1.xml").iter(qn("w:t")))
    assert "K. Ito" in header and "{{name}}" not in header
    assert check_integrity(reopened) == []


def test_an_unsupported_image_format_leaves_the_marker_untouched():
    # A format Word cannot embed (WebP/SVG/HEIC) must not be reported as filled
    # and must leave the marker in place, not silently swallow it.
    pkg = build.make(body=build.para("Logo: {{image: logo}}", style="BodyText"))
    report = fill(pkg, {"logo": b"RIFF\x00\x00\x00\x00WEBPVP8 "})
    assert "logo" not in report.filled
    assert "logo" in report.unknown
    assert "{{image: logo}}" in text_of(pkg)
    assert check_integrity(pkg) == []
