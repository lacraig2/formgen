"""Fields a document declares about itself, and filling them in.

Every case here came off a real document: a consular visa application built
out of legacy form fields, a Czech letter template using ###MARKER###
conventions, and a Danish mail merge. The shapes are theirs; only the words
are fixtures.
"""

from __future__ import annotations


from fixtures import build
from formgen.content.fill import fill
from formgen.learn.formfields import find_fields, slug
from formgen.opc.ns import NS, qn
from formgen.opc.package import OpcPackage
from formgen.oox.walk import Walker


def visa_form(passport: str = "", name_value: str = "") -> str:
    """The shape of a real form: labels above, fields below, boxes in a row."""
    return (
        build.para("VISA APPLICATION FORM")
        + build.table(
            build.cell(build.para("01 -  Full name"))
            + build.cell(build.para("07 -   Passport #")),
            build.cell(build.para("", runs=build.form_text("Text1", name_value)))
            + build.cell(build.para("", runs=build.form_text("Text42", passport))),
        )
        + build.para("05 -   Sex")
        + build.para("", runs=(
            build.form_checkbox("Check1") + '<w:r><w:t>male</w:t></w:r>'
            + build.form_checkbox("Check2") + '<w:r><w:t>female</w:t></w:r>'))
        + build.para("", runs=(
            build.form_checkbox("Check3") + '<w:r><w:t>no diploma</w:t></w:r>'))
    )


def names(pkg):
    return [f.name for f in find_fields(pkg).fields]


def texts(pkg):
    return [b.text for b in Walker(pkg).blocks(include_aux=False) if b.text.strip()]


# -- finding them --------------------------------------------------------


def test_a_legacy_form_field_is_found_and_named_from_the_label_above_it():
    """Word calls it Text42. The form calls it "07 - Passport #", which is
    the only one of the two a person could use."""
    found = {f.name: f for f in find_fields(build.make(visa_form())).fields}
    assert "passport" in found
    assert found["passport"].source == "ffData"
    assert found["passport"].raw_name == "Text42"
    assert found["passport"].needs_review


def test_a_question_number_is_not_part_of_the_name():
    assert slug("01 -  Full name") == "full_name"
    assert slug("3. Date of birth") == "date_of_birth"
    assert slug("12) Sex") == "sex"


def test_each_check_box_takes_its_own_label_not_the_whole_line():
    found = names(build.make(visa_form()))
    assert "male" in found and "female" in found


def test_a_check_box_alone_in_its_paragraph_still_finds_its_label():
    """The commonest shape of all: one option per line down a page."""
    assert "no_diploma" in names(build.make(visa_form()))


def test_labels_printed_before_the_boxes_work_too():
    """Word's Forms toolbar puts the label after the box; the consular form
    puts it before. The paragraph says which by where its text runs out."""
    body = build.para("", runs=(
        '<w:r><w:t>yes</w:t></w:r>' + build.form_checkbox("Check1")
        + '<w:r><w:t>no</w:t></w:r>' + build.form_checkbox("Check2")))
    assert names(build.make(body)) == ["yes", "no"]


def test_a_meaningful_field_name_beats_the_label():
    body = build.para("Some nearby words") + build.para(
        "", runs=build.form_text("ApplicantSurname"))
    found = find_fields(build.make(body)).fields[0]
    assert found.name == "applicantsurname"
    assert found.name_confidence >= 0.9


def test_text_markers_are_found():
    """A hand-made convention, so low confidence and flagged -- but found."""
    body = build.para("###Surname/Company###") + build.para("Praha ###DATUM###")
    found = {f.name: f for f in find_fields(build.make(body)).fields}
    assert set(found) == {"surname_company", "datum"}
    assert all(f.needs_review for f in found.values())


def test_a_mergefield_names_itself():
    body = build.para("", runs=(
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> MERGEFIELD Fornavn </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t>«Fornavn»</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'))
    found = find_fields(build.make(body)).fields
    assert found[0].name == "fornavn"
    assert found[0].name_confidence >= 0.9


def test_an_inline_content_control_is_found():
    """The shape `learn` itself writes: a few words mid-line, not a block."""
    body = build.para("Report No. ", runs=(
        f'<w:sdt xmlns:w="{NS["w"]}"><w:sdtPr>'
        '<w:tag w:val="formgen.report_number"/><w:text/></w:sdtPr>'
        '<w:sdtContent><w:r><w:t>LR-2024-0041</w:t></w:r></w:sdtContent></w:sdt>'))
    found = find_fields(build.make(body)).fields
    assert found[0].name == "report_number"
    assert found[0].name_confidence == 1.0
    assert found[0].value == "LR-2024-0041"


def test_a_documents_own_value_is_reported_not_its_reset_default():
    """`w:default` is what Word restores; the result is what somebody typed,
    and only the result answers "is this filled in?"."""
    body = build.para("Passport") + build.para("", runs=build.form_text(
        "Text1", result="PT-4471902", default="enter number"))
    found = find_fields(build.make(body)).fields[0]
    assert found.value == "PT-4471902"
    assert found.filled


def test_field_names_do_not_change_when_the_form_is_filled_in():
    """The property that matters when you start from filled documents: a
    text field named after its own contents is renamed by every applicant."""
    blank = names(build.make(visa_form()))
    filled = names(build.make(visa_form(passport="PT-4471902",
                                        name_value="Ada Lovelace")))
    assert blank == filled


def test_a_document_with_no_fields_says_so_rather_than_inventing_any():
    assert find_fields(build.make()).fields == []


# -- filling them --------------------------------------------------------


def test_filling_keeps_the_whole_form_and_changes_only_the_values():
    """A form is the opposite of a report: the labels, the table and the
    layout *are* the document, and the values are the small part."""
    pkg = build.make(visa_form())
    report = fill(pkg, {"passport": "PT-4471902", "full_name": "Ada Lovelace",
                        "male": True})
    assert set(report.filled) >= {"passport", "full_name", "male"}
    body = texts(pkg)
    assert "VISA APPLICATION FORM" in body
    assert any("01 -  Full name" in t for t in body)
    assert any("PT-4471902" in t for t in body)


def test_a_filled_form_survives_a_save_and_reopen(tmp_path):
    pkg = build.make(visa_form())
    fill(pkg, {"passport": "PT-4471902", "female": True})
    path = tmp_path / "filled.docx"
    pkg.save(path, deterministic=True)

    found = {f.name: f for f in find_fields(OpcPackage.open(path)).fields}
    assert found["passport"].value == "PT-4471902"
    assert found["female"].value == "checked"
    assert not found["male"].filled


def test_an_edited_tree_that_is_never_marked_dirty_would_be_thrown_away(tmp_path):
    """Untouched parts are written back byte for byte -- that is the whole
    preservation guarantee, and it means fill must mark the part."""
    pkg = build.make(visa_form())
    fill(pkg, {"passport": "PT-4471902"})
    assert pkg.is_dirty(pkg.main_document)


def test_a_field_nobody_supplied_is_cleared_not_left_as_it_was():
    """The template is a byte-faithful copy of a real document. Leaving its
    values is how one person's form ships with another's passport number."""
    pkg = build.make(visa_form(passport="PT-4471902"))
    report = fill(pkg, {"full_name": "Ada Lovelace"})
    assert "passport" in report.cleared
    assert not any("PT-4471902" in t for t in texts(pkg))


def test_keep_unsupplied_is_available_when_it_is_the_users_own_document():
    pkg = build.make(visa_form(passport="PT-4471902"))
    fill(pkg, {"full_name": "Ada Lovelace"}, keep_unsupplied=True)
    assert any("PT-4471902" in t for t in texts(pkg))


def test_a_value_matching_no_field_is_reported_rather_than_dropped():
    pkg = build.make(visa_form())
    report = fill(pkg, {"passport": "x", "there_is_no_such_field": "y"})
    assert report.unknown == ["there_is_no_such_field"]


def test_a_check_box_is_ticked_in_the_markup_not_just_in_the_text():
    pkg = build.make(visa_form())
    fill(pkg, {"male": True, "female": False})
    body = pkg.element(pkg.main_document)
    states = [c.get(qn("w:val")) for c in body.iter(qn("w:checked"))]
    assert states.count(None) == 1          # exactly one ticked
    assert states.count("0") == len(states) - 1


def test_markers_are_substituted_in_place():
    pkg = build.make(build.para("Praha ###DATUM###, ###City###"))
    fill(pkg, {"datum": "15 September 2026", "city": "Praha"})
    assert texts(pkg) == ["Praha 15 September 2026, Praha"]


def test_a_filled_document_still_passes_its_structural_checks():
    from formgen.safety.verify import check_integrity

    pkg = build.make(visa_form())
    fill(pkg, {"passport": "PT-4471902", "male": True})
    assert check_integrity(pkg) == []


def test_filling_twice_with_the_same_values_changes_nothing(tmp_path):
    def once():
        pkg = build.make(visa_form())
        fill(pkg, {"passport": "PT-4471902", "male": True})
        return pkg

    first, second = tmp_path / "a.docx", tmp_path / "b.docx"
    once().save(first, deterministic=True)
    pkg = OpcPackage.open(first)
    fill(pkg, {"passport": "PT-4471902", "male": True})
    pkg.save(second, deterministic=True)
    assert first.read_bytes() == second.read_bytes()
