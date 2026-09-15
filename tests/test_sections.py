"""Sections: where a w:sectPr lives, and which header a page really gets."""

from __future__ import annotations

from lxml import etree

from fixtures import build
from formgen.oox.sections import SectionModel
from formgen.oox.settings import Settings
from formgen.opc.ns import RT, qn


def body_of(xml: str) -> etree._Element:
    return etree.fromstring(xml.encode()).find(qn("w:body"))


def model(body_xml: str, sect_pr: str | None = None, **settings) -> SectionModel:
    doc = build.document(body_xml, sect_pr)
    return SectionModel.from_body(body_of(doc), Settings(**settings))


# -- discovery ------------------------------------------------------------


def test_a_single_section_document_has_one_section_at_body_level():
    m = model(build.para("Hello"))
    assert len(m) == 1
    assert m[0].is_final
    assert m[0].page.width.inch == 8.5
    assert m[0].margins.left.inch == 1.0


def test_a_section_break_paragraph_ends_the_section_it_carries():
    """The w:sectPr describes the section ENDING at that paragraph."""
    first = build.sectpr(orient="landscape", w=15840, h=12240)
    m = model(
        build.para("cover")
        + build.break_para(first)
        + build.para("body")
    )
    assert len(m) == 2
    assert m[0].page.orientation == "landscape"
    assert m[0].owner_paragraph is not None
    assert m[1].is_final and m[1].page.orientation == "portrait"


def test_blocks_map_to_the_section_that_ends_after_them():
    first = build.sectpr()
    doc = build.document(
        build.para("cover") + build.break_para(first, "last of section 0")
        + build.para("body")
    )
    root = etree.fromstring(doc.encode())
    m = SectionModel.from_body(root.find(qn("w:body")))
    paras = root.findall(f"{qn('w:body')}/{qn('w:p')}")
    assert [m.section_of(p) for p in paras] == [0, 0, 1]


def test_a_paragraph_in_a_table_reports_its_tables_section():
    tbl = (
        "<w:tbl><w:tr><w:tc>" + build.para("cell") + "</w:tc></w:tr></w:tbl>"
    )
    # The table sits in section 0, so falling through to "the last section"
    # would give the wrong answer -- which is the point of the test.
    doc = build.document(
        tbl + build.break_para(build.sectpr()) + build.para("after")
    )
    root = etree.fromstring(doc.encode())
    m = SectionModel.from_body(root.find(qn("w:body")))
    cell_para = root.find(f"{qn('w:body')}/{qn('w:tbl')}//{qn('w:p')}")
    assert m.section_of(cell_para) == 0
    assert len(m) == 2


def test_a_section_break_inside_a_tracked_insertion_still_splits():
    """Rare, but merging two sections into one is a silent page-setup loss."""
    inner = build.break_para(build.sectpr(orient="landscape", w=15840, h=12240))
    m = model(build.para("a") + f"<w:ins>{inner}</w:ins>" + build.para("b"))
    assert len(m) == 2
    assert m[0].page.orientation == "landscape"


def test_a_section_break_inside_a_content_control_still_splits():
    inner = build.break_para(build.sectpr())
    m = model(
        build.para("a")
        + f"<w:sdt><w:sdtPr/><w:sdtContent>{inner}</w:sdtContent></w:sdt>"
        + build.para("b")
    )
    assert len(m) == 2


def test_a_body_with_no_final_sectpr_still_yields_one_section():
    """Invalid, but HTML-to-docx converters emit it; indices must stay valid."""
    m = model(build.para("orphan"), sect_pr="")
    assert len(m) == 1
    assert m[0].page.width is None
    assert any("no w:pgSz" in p for p in m.problems())


def test_blocks_after_the_body_level_sectpr_are_reported_as_corruption():
    doc = build.document("", build.sectpr() + build.para("stray"))
    m = SectionModel.from_body(body_of(doc))
    assert m.trailing_blocks == 1
    assert any("follow the body-level w:sectPr" in p for p in m.problems())


# -- geometry -------------------------------------------------------------


def test_content_width_subtracts_both_margins_and_the_gutter():
    m = model(
        build.para("x"),
        build.sectpr(
            margins='w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
                    'w:header="720" w:footer="720" w:gutter="720"'
        ),
    )
    assert m[0].content_width.twips == 12240 - 1440 - 1440 - 720
    assert m[0].content_height.twips == 15840 - 1440 - 1440


def test_orientation_is_inferred_when_the_attribute_is_missing():
    """Producers that swap w/h without writing w:orient are common."""
    m = model(build.para("x"), build.sectpr(w=15840, h=12240))
    assert m[0].page.orientation == "landscape"


def test_explicit_orient_attribute_wins():
    m = model(build.para("x"), build.sectpr(w=12240, h=15840, orient="landscape"))
    assert m[0].page.orientation == "landscape"


def test_equal_columns_split_the_text_width_minus_the_gaps():
    m = model(
        build.para("x"),
        build.sectpr(cols='<w:cols w:num="2" w:space="720"/>'),
    )
    assert m[0].columns.count == 2
    assert m[0].columns.equal_width is True
    assert m[0].column_width.twips == (9360 - 720) // 2


def test_unequal_columns_take_their_declared_widths():
    m = model(
        build.para("x"),
        build.sectpr(
            cols='<w:cols w:num="2" w:equalWidth="0" w:sep="1">'
                 '<w:col w:w="6000" w:space="360"/><w:col w:w="3000"/></w:cols>'
        ),
    )
    cols = m[0].columns
    assert cols.equal_width is False and cols.separator is True
    assert [c.twips for c in cols.widths] == [6000, 3000]


def test_negative_bottom_margin_survives_parsing():
    """w:top and w:bottom are signed; clamping them hides a real layout."""
    m = model(
        build.para("x"),
        build.sectpr(
            margins='w:top="1440" w:right="1440" w:bottom="-720" w:left="1440"'
        ),
    )
    assert m[0].margins.bottom.twips == -720


def test_margins_wider_than_the_page_are_reported():
    m = model(
        build.para("x"),
        build.sectpr(margins='w:top="1440" w:right="7000" w:bottom="1440" w:left="7000"'),
    )
    assert any("no text width" in p for p in m.problems())


def test_page_setup_signature_ignores_rids_and_part_names():
    a = model(build.para("x"), build.sectpr(headers={"default": "rId7"}))
    b = model(build.para("x"), build.sectpr(headers={"default": "rId99"}))
    assert a[0].signature() == b[0].signature()


# -- headers and footers --------------------------------------------------


def with_headers() -> tuple:
    """A two-section package where only section 0 declares a header."""
    pkg = build.make(
        body=build.para("cover") + build.break_para(build.sectpr(title_page=True)),
        sect_pr=build.sectpr(),
    )
    rid = build.add_hdrftr(pkg, "header", "header1.xml", "HOUSE REPORT")
    # Point section 0 at it after the fact, so the rId is the real one.
    doc = pkg.edit(pkg.main_document)
    first = doc.find(f"{qn('w:body')}/{qn('w:p')}/{qn('w:pPr')}/{qn('w:sectPr')}")
    ref = etree.SubElement(first, qn("w:headerReference"))
    ref.set(qn("w:type"), "default")
    ref.set(qn("r:id"), rid)
    first.insert(0, ref)
    return pkg, rid


def test_an_omitted_header_reference_links_to_the_previous_section():
    """The trap: absence means "same as before", never "none"."""
    pkg, _ = with_headers()
    m = SectionModel.from_package(pkg)
    assert len(m) == 2
    own = m.resolve(0, "header", "default")
    assert own.part == "word/header1.xml" and own.inherited is False

    inherited = m.resolve(1, "header", "default")
    assert inherited.part == "word/header1.xml"
    assert inherited.inherited is True and inherited.defined_in == 0
    assert [r.type for r in m.linked_to_previous(1)] == ["default"]


def test_a_first_section_with_no_header_reference_renders_blank():
    pkg = build.make()
    m = SectionModel.from_package(pkg)
    ref = m.resolve(0, "header", "default")
    assert ref.part is None and ref.inherited is False


def test_first_page_slot_is_dead_without_titlepg():
    pkg, _ = with_headers()
    m = SectionModel.from_package(pkg)
    assert "first" in m.active_types(0)      # the break paragraph set titlePg
    assert "first" not in m.active_types(1)


def test_even_slot_needs_a_document_wide_setting_not_a_section_one():
    """w:evenAndOddHeaders lives in settings.xml; w:titlePg does not."""
    pkg = build.make()
    m = SectionModel.from_package(pkg)
    assert m.active_types(0) == ("default",)

    pkg2 = build.make(settings_extra="<w:evenAndOddHeaders/>")
    m2 = SectionModel.from_package(pkg2)
    assert m2.active_types(0) == ("even", "default")


def test_an_even_header_is_an_orphan_when_the_setting_is_off():
    """Left behind after turning off Different Odd & Even Pages: invisible."""
    pkg = build.make()
    rid = build.add_hdrftr(pkg, "header", "header2.xml", "EVEN")
    sect = pkg.edit(pkg.main_document).find(f"{qn('w:body')}/{qn('w:sectPr')}")
    ref = etree.SubElement(sect, qn("w:headerReference"))
    ref.set(qn("w:type"), "even")
    ref.set(qn("r:id"), rid)
    sect.insert(0, ref)

    m = SectionModel.from_package(pkg)
    assert m.orphan_parts() == ["word/header2.xml"]
    assert m.resolve(0, "header", "even").active is False


def test_a_header_reference_without_a_type_means_default():
    pkg = build.make()
    rid = build.add_hdrftr(pkg, "header", "header1.xml", "H")
    sect = pkg.edit(pkg.main_document).find(f"{qn('w:body')}/{qn('w:sectPr')}")
    ref = etree.Element(qn("w:headerReference"))
    ref.set(qn("r:id"), rid)
    sect.insert(0, ref)
    m = SectionModel.from_package(pkg)
    assert m.resolve(0, "header", "default").part == "word/header1.xml"


def test_even_and_odd_headers_without_an_even_part_is_reported():
    pkg = build.make(settings_extra="<w:evenAndOddHeaders/>")
    rid = build.add_hdrftr(pkg, "header", "header1.xml", "H")
    sect = pkg.edit(pkg.main_document).find(f"{qn('w:body')}/{qn('w:sectPr')}")
    ref = etree.SubElement(sect, qn("w:headerReference"))
    ref.set(qn("w:type"), "default")
    ref.set(qn("r:id"), rid)
    sect.insert(0, ref)
    m = SectionModel.from_package(pkg)
    assert any("render blank" in p for p in m.problems())


def test_headers_and_footers_inherit_independently():
    """Word lets you unlink one without unlinking the other."""
    m = model(
        build.para("a")
        + build.break_para(build.sectpr(headers={"default": "rIdH"},
                                        footers={"default": "rIdF"}))
        + build.para("b"),
        build.sectpr(footers={"default": "rIdF2"}),
    )
    assert m.resolve(1, "header", "default").inherited is True
    assert m.resolve(1, "footer", "default").inherited is False


# -- settings -------------------------------------------------------------


def test_settings_reads_the_switches_that_gate_writing():
    pkg = build.make(
        settings_extra='<w:trackChanges/>'
        '<w:documentProtection w:edit="readOnly" w:enforcement="1"/>'
    )
    s = Settings.parse(pkg.element(pkg.related(RT["settings"])))
    assert s.track_changes is True
    assert s.protection.blocks_editing is True
    assert len(s.refusal_reasons) == 2
    assert all("re-run" in r or "run against" in r or "Stop Protection" in r
               for r in s.refusal_reasons)


def test_unenforced_protection_is_not_a_refusal():
    pkg = build.make(
        settings_extra='<w:documentProtection w:edit="readOnly" w:enforcement="0"/>'
    )
    s = Settings.parse(pkg.element(pkg.related(RT["settings"])))
    assert s.protection.enforced is False
    assert s.refusal_reasons == []


def test_default_tab_stop_and_compat_mode_are_read():
    pkg = build.make()
    s = Settings.parse(pkg.element(pkg.related(RT["settings"])))
    assert s.default_tab_stop.inch == 0.5
    assert s.compat_mode == 15
