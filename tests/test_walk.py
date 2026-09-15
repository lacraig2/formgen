"""Block walker: reaching content that naive w:body iteration silently skips."""

from __future__ import annotations

from fixtures.build import make, para
from formgen.opc.ns import NS
from formgen.oox.walk import (
    document_text,
    exact_key,
    has_alt_chunks,
    match_key,
    field_instructions,
    iter_runs,
    normalize_text,
    paragraph_text,
    walk,
)
from lxml import etree

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
NS_M = NS["m"]
WPS = 'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'


def p_el(xml: str):
    return etree.fromstring(f"<w:p {W}>{xml}</w:p>".encode())


def texts(pkg):
    return [b.text for b in walk(pkg) if b.is_paragraph and b.text]


# -- tables --------------------------------------------------------------

TABLE = f"""<w:tbl>
<w:tblPr><w:tblStyle w:val="LabDataTable"/></w:tblPr>
<w:tr><w:tc>{para("Case")}</w:tc><w:tc>{para("Margin")}</w:tc></w:tr>
<w:tr><w:tc>{para("Nominal")}</w:tc><w:tc>{para("12.4")}</w:tc></w:tr>
</w:tbl>"""


def test_paragraphs_inside_table_cells_are_reached():
    pkg = make(body=para("Before") + TABLE + para("After"))
    assert texts(pkg) == ["Before", "Case", "Margin", "Nominal", "12.4", "After"]


def test_table_cell_context_records_row_and_column():
    pkg = make(body=TABLE)
    cells = [b for b in walk(pkg) if b.is_paragraph]
    assert cells[0].context.describe() == "table row 1, column 1"
    assert cells[3].context.describe() == "table row 2, column 2"


def test_table_block_exposes_its_style():
    pkg = make(body=TABLE)
    tbl = next(b for b in walk(pkg) if b.kind == "tbl")
    assert tbl.style_id == "LabDataTable"


def test_nested_tables_record_depth():
    inner = f"<w:tbl><w:tr><w:tc>{para('deep')}</w:tc></w:tr></w:tbl>"
    outer = f"<w:tbl><w:tr><w:tc>{inner}</w:tc></w:tr></w:tbl>"
    pkg = make(body=outer)
    # The outer table block's text also reads "deep"; we want the paragraph.
    deep = next(b for b in walk(pkg) if b.is_paragraph and b.text == "deep")
    assert deep.context.table_depth == 2
    assert "nested 2 deep" in deep.context.describe()


# -- content controls, text boxes, tracked insertions --------------------


def test_paragraphs_inside_a_content_control_are_reached_and_tagged():
    sdt = (f'<w:sdt><w:sdtPr><w:tag w:val="formgen.title"/></w:sdtPr>'
           f"<w:sdtContent>{para('Thermal Analysis')}</w:sdtContent></w:sdt>")
    pkg = make(body=sdt)
    block = next(b for b in walk(pkg) if b.is_paragraph)
    assert block.text == "Thermal Analysis"
    assert block.context.in_sdt and block.context.sdt_tag == "formgen.title"


def test_paragraphs_inside_a_text_box_are_reached():
    box = (f'<w:p><w:r><mc:AlternateContent {MC} {WPS}><mc:Choice Requires="wps">'
           f"<w:txbxContent>{para('Cover title in a shape')}</w:txbxContent>"
           f"</mc:Choice></mc:AlternateContent></w:r></w:p>")
    pkg = make(body=box)
    found = [b for b in walk(pkg) if b.text == "Cover title in a shape"]
    assert len(found) == 1
    assert found[0].context.in_textbox


def test_alternate_content_is_not_counted_twice():
    """mc:Choice and mc:Fallback duplicate the same content; take exactly one."""
    dual = (f'<w:p><w:r><mc:AlternateContent {MC} {WPS}>'
            f"<mc:Choice Requires=\"wps\"><w:txbxContent>{para('Boxed')}</w:txbxContent></mc:Choice>"
            f"<mc:Fallback><w:txbxContent>{para('Boxed')}</w:txbxContent></mc:Fallback>"
            f"</mc:AlternateContent></w:r></w:p>")
    pkg = make(body=dual)
    assert [b.text for b in walk(pkg) if b.text == "Boxed"] == ["Boxed"]


def test_tracked_insertion_content_is_reached_and_flagged():
    pkg = make(body=f'<w:ins w:id="1" w:author="a">{para("inserted")}</w:ins>')
    block = next(b for b in walk(pkg) if b.text == "inserted")
    assert block.context.in_ins


# -- runs and text -------------------------------------------------------


def test_hyperlink_text_is_not_lost():
    """A walker that only looks at ./w:r loses every hyperlink."""
    p = p_el('<w:r><w:t>See </w:t></w:r>'
             '<w:hyperlink r:id="rId9" '
             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
             '<w:r><w:t>the manual</w:t></w:r></w:hyperlink>')
    assert paragraph_text(p) == "See the manual"
    assert len(list(iter_runs(p))) == 2


def test_tabs_and_breaks_become_text():
    p = p_el("<w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r>")
    assert paragraph_text(p) == "a\tb\nc"


def test_deleted_text_is_not_visible():
    p = p_el("<w:r><w:t>kept</w:t></w:r>"
             '<w:del w:id="2"><w:r><w:delText>gone</w:delText></w:r></w:del>')
    assert paragraph_text(p) == "kept"


def test_instruction_text_is_not_visible_text():
    p = p_el('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
             '<w:r><w:instrText>PAGE</w:instrText></w:r>'
             '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
             '<w:r><w:t>7</w:t></w:r>'
             '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    assert paragraph_text(p) == "7"


# -- fields --------------------------------------------------------------


def test_field_instruction_split_across_runs_is_reassembled():
    """Word splits DOCPROPERTY "ProjectNumber" across arbitrary run boundaries."""
    p = p_el('<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
             '<w:r><w:instrText xml:space="preserve">DOCPROP</w:instrText></w:r>'
             '<w:r><w:instrText xml:space="preserve">ERTY "Proj</w:instrText></w:r>'
             '<w:r><w:instrText xml:space="preserve">ectNumber"</w:instrText></w:r>'
             '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
             '<w:r><w:t>P-1234</w:t></w:r>'
             '<w:r><w:fldChar w:fldCharType="end"/></w:r>')
    assert field_instructions(p) == ['DOCPROPERTY "ProjectNumber"']


def test_simple_field_form_is_also_read():
    p = p_el('<w:fldSimple w:instr=" SEQ Figure \\* ARABIC "><w:r><w:t>3</w:t></w:r></w:fldSimple>')
    assert field_instructions(p) == ["SEQ Figure \\* ARABIC"]


def test_paragraph_without_fields_reports_none():
    assert field_instructions(p_el("<w:r><w:t>plain</w:t></w:r>")) == []


# -- headers, footers, footnotes ----------------------------------------


def test_footnote_bodies_are_walked_but_separators_are_not():
    notes = (
        '<?xml version="1.0"?>'
        f'<w:footnotes {W}>'
        f'<w:footnote w:type="separator" w:id="-1">{para("")}</w:footnote>'
        f'<w:footnote w:type="continuationSeparator" w:id="0">{para("")}</w:footnote>'
        f'<w:footnote w:id="2">{para("Measured at 340 K.")}</w:footnote>'
        "</w:footnotes>"
    )
    pkg = make(extra_parts={"word/footnotes.xml": notes.encode()})
    rels = pkg.touch_rels(pkg.main_document)
    rels.add("http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes",
             "footnotes.xml")
    pkg.content_types.set_override("word/footnotes.xml", "ct/footnotes")

    notes_blocks = [b for b in walk(pkg) if b.context.kind == "footnote"]
    assert [b.text for b in notes_blocks] == ["Measured at 340 K."]
    assert notes_blocks[0].context.describe() == "footnote"


def test_excluding_aux_parts_limits_the_walk():
    pkg = make()
    assert len(walk(pkg, include_aux=False)) == len(walk(pkg, include_aux=True))


# -- normalization -------------------------------------------------------


def test_invisible_differences_normalize_away():
    """Boilerplate detection fails on these without normalization."""
    assert normalize_text("Lincoln Laboratory") == "Lincoln Laboratory"
    assert normalize_text("non‑breaking") == "non-breaking"
    assert normalize_text("soft­hyphen") == "softhyphen"
    assert normalize_text("“quoted”") == '"quoted"'
    assert normalize_text("it’s") == "it's"
    assert normalize_text("  spaced   out \n") == "spaced out"


def test_normalization_makes_visually_identical_text_compare_equal():
    a = normalize_text("Approved for public release—distribution unlimited.")
    b = normalize_text("Approved for public release-distribution unlimited.")
    assert a == b


# -- whole-document text -------------------------------------------------


def test_document_text_includes_table_and_box_content():
    box = (f'<w:p><w:r><mc:AlternateContent {MC} {WPS}><mc:Choice Requires="wps">'
           f"<w:txbxContent>{para('in a box')}</w:txbxContent>"
           f"</mc:Choice></mc:AlternateContent></w:r></w:p>")
    pkg = make(body=para("body") + TABLE + box)
    text = document_text(pkg)
    for fragment in ["body", "Nominal", "in a box"]:
        assert fragment in text


# -- wrapped rows and cells (Word's repeating-section control) -----------


def test_row_level_content_control_does_not_lose_the_row():
    """w:tbl holds EG_ContentRowContent: a row may be wrapped in w:sdt.

    This is what Word's repeating-section control emits, so filtering with
    findall(w:tr) loses whole table bodies in any form template.
    """
    tbl = (f"<w:tbl><w:tblPr/>"
           f"<w:tr><w:tc>{para('plain row')}</w:tc></w:tr>"
           f'<w:sdt><w:sdtPr><w:tag w:val="repeat"/></w:sdtPr><w:sdtContent>'
           f"<w:tr><w:tc>{para('ROW INSIDE SDT')}</w:tc></w:tr>"
           f"</w:sdtContent></w:sdt></w:tbl>")
    pkg = make(body=tbl)
    assert "ROW INSIDE SDT" in texts(pkg)
    assert "ROW INSIDE SDT" in document_text(pkg)


def test_cell_level_content_control_does_not_lose_the_cell():
    tbl = (f"<w:tbl><w:tblPr/><w:tr>"
           f"<w:tc>{para('plain cell')}</w:tc>"
           f"<w:sdt><w:sdtContent><w:tc>{para('CELL IN SDT')}</w:tc></w:sdtContent></w:sdt>"
           f"</w:tr></w:tbl>")
    pkg = make(body=tbl)
    assert "CELL IN SDT" in texts(pkg)


def test_custom_xml_wrapped_rows_are_reached():
    tbl = (f'<w:tbl><w:tblPr/><w:customXml w:element="rows">'
           f"<w:tr><w:tc>{para('WRAPPED')}</w:tc></w:tr></w:customXml></w:tbl>")
    assert "WRAPPED" in texts(make(body=tbl))


# -- tracked changes -----------------------------------------------------


def test_move_destination_text_is_kept():
    """w:moveTo is the destination of a drag; it is the final visible text."""
    p = p_el('<w:r><w:t>a </w:t></w:r>'
             '<w:moveTo w:id="1"><w:r><w:t>MOVED HERE</w:t></w:r></w:moveTo>'
             '<w:r><w:t> b</w:t></w:r>')
    assert paragraph_text(p) == "a MOVED HERE b"


def test_move_source_text_is_not_double_counted():
    """w:moveFrom runs carry ordinary w:t, so recursing duplicates the move."""
    p = p_el('<w:moveFrom w:id="1"><w:r><w:t>MOVED</w:t></w:r></w:moveFrom>'
             '<w:moveTo w:id="2"><w:r><w:t>MOVED</w:t></w:r></w:moveTo>')
    assert paragraph_text(p) == "MOVED"


def test_block_level_move_destination_yields_a_block():
    pkg = make(body=f'<w:moveTo w:id="1">{para("moved para")}</w:moveTo>')
    assert "moved para" in texts(pkg)


def test_deleted_paragraphs_are_yielded_but_flagged_and_excluded_from_text():
    """A reformatter must see them; a text invariant must not."""
    pkg = make(body=f'<w:del w:id="1">{para("deleted para")}</w:del>'
                    + para("kept", style="BodyText"))
    deleted = [b for b in walk(pkg) if b.context.in_del]
    assert len(deleted) == 1
    assert "deleted para" not in document_text(pkg)
    assert "kept" in document_text(pkg)


def test_block_level_custom_xml_is_transparent():
    pkg = make(body=f'<w:customXml w:element="foo">{para("INSIDE CUSTOMXML")}</w:customXml>')
    assert "INSIDE CUSTOMXML" in texts(pkg)


# -- markup compatibility ------------------------------------------------


def test_first_understood_choice_wins_not_simply_the_first():
    """Requires names namespace PREFIXES, which are file-local."""
    dual = (f'<w:p><w:r><mc:AlternateContent {MC} {WPS} xmlns:future="urn:not-real">'
            f'<mc:Choice Requires="future"><w:txbxContent>{para("FUTURE")}</w:txbxContent></mc:Choice>'
            f'<mc:Choice Requires="wps"><w:txbxContent>{para("WPS")}</w:txbxContent></mc:Choice>'
            f'<mc:Fallback><w:txbxContent>{para("FALLBACK")}</w:txbxContent></mc:Fallback>'
            f"</mc:AlternateContent></w:r></w:p>")
    found = [t for t in texts(make(body=dual)) if t in ("FUTURE", "WPS", "FALLBACK")]
    assert found == ["WPS"]


def test_inline_alternate_content_text_is_not_dropped():
    """Skipping AlternateContent wholesale loses symbol swaps and wrapped runs."""
    p = p_el(f'<w:r><w:t>before </w:t></w:r>'
             f'<mc:AlternateContent {MC}>'
             f'<mc:Choice Requires="w14" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
             f'<w:r><w:t>INLINE</w:t></w:r></mc:Choice>'
             f'<mc:Fallback><w:r><w:t>FALLBACK</w:t></w:r></mc:Fallback>'
             f'</mc:AlternateContent>'
             f'<w:r><w:t> after</w:t></w:r>')
    assert paragraph_text(p) == "before INLINE after"


# -- math, symbols, imported content -------------------------------------


def test_office_math_contributes_text():
    p = p_el('<w:r><w:t>where </w:t></w:r>'
             f'<m:oMath xmlns:m="{NS_M}"><m:r><m:t>x+1</m:t></m:r></m:oMath>')
    assert paragraph_text(p) == "where x+1"


def test_display_equation_is_addressable_as_a_block():
    pkg = make(body=f'<m:oMathPara xmlns:m="{NS_M}"><m:oMath><m:r><m:t>E=mc2</m:t>'
                    f"</m:r></m:oMath></m:oMathPara>")
    assert any(b.kind == "oMathPara" for b in walk(pkg))


def test_symbol_runs_contribute_a_placeholder_not_nothing():
    """Dropping w:sym silently drifts every character-count invariant."""
    p = p_el('<w:r><w:sym w:font="Wingdings" w:char="F0FC"/></w:r>')
    assert paragraph_text(p) == "￼"


def test_position_tab_is_a_tab():
    p = p_el('<w:r><w:t>Title</w:t><w:ptab w:relativeTo="margin" '
             'w:alignment="right" w:leader="dot"/><w:t>4</w:t></w:r>')
    assert paragraph_text(p) == "Title\t4"


def test_alt_chunk_is_surfaced_so_lint_can_refuse_to_certify():
    """Word renders the imported part; we cannot inspect or restyle it."""
    pkg = make(body=para("before") + '<w:altChunk r:id="rId99"/>' + para("after"))
    assert has_alt_chunks(pkg)
    assert any(b.kind == "altChunk" for b in walk(pkg))


# -- footnotes -----------------------------------------------------------


def test_separator_notes_are_filtered_by_type_not_by_id():
    """Other producers number the first real note 0; filtering by id loses it."""
    notes = (
        '<?xml version="1.0"?>'
        f'<w:footnotes {W}>'
        f'<w:footnote w:type="separator" w:id="7">{para("SEP")}</w:footnote>'
        f'<w:footnote w:id="0">{para("real note numbered zero")}</w:footnote>'
        "</w:footnotes>"
    )
    pkg = make(extra_parts={"word/footnotes.xml": notes.encode()})
    pkg.touch_rels(pkg.main_document).add(
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes",
        "footnotes.xml")
    pkg.content_types.set_override("word/footnotes.xml", "ct/footnotes")
    found = [b.text for b in walk(pkg) if b.context.kind == "footnote"]
    assert found == ["real note numbered zero"]


# -- locators ------------------------------------------------------------


def test_block_paths_are_unique():
    """A duplicate path means lint points at the wrong paragraph."""
    body = (para("first")
            + f'<w:ins w:id="1">{para("ins one")}</w:ins>'
            + para("third")
            + f"<w:sdt><w:sdtContent>{para('A')}</w:sdtContent></w:sdt>"
            + f"<w:sdt><w:sdtContent>{para('B')}</w:sdtContent></w:sdt>")
    blocks = walk(make(body=body))
    paths = [b.path for b in blocks]
    assert len(set(paths)) == len(paths)


def test_inserted_paragraphs_continue_the_body_numbering():
    """Matches what a human counts in Word, and keeps paths unique."""
    body = para("first") + f'<w:ins w:id="1">{para("second")}</w:ins>' + para("third")
    paths = [b.path for b in walk(make(body=body)) if b.is_paragraph]
    assert paths == ["body/p[1]", "body/p[2]", "body/p[3]"]


def test_para_id_is_captured_when_present():
    pkg = make(body='<w:p w14:paraId="1A2B3C4D" '
                    'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
                    "<w:r><w:t>x</w:t></w:r></w:p>")
    assert next(b for b in walk(pkg) if b.is_paragraph).para_id == "1A2B3C4D"


# -- content control placeholders ----------------------------------------


def test_placeholder_prompt_text_is_not_document_text():
    """An empty control holds Word's greyed prompt; it is not a filled field."""
    sdt = ('<w:sdt><w:sdtPr><w:tag w:val="formgen.title"/><w:showingPlcHdr/>'
           f"</w:sdtPr><w:sdtContent>{para('[Click to enter title]')}"
           "</w:sdtContent></w:sdt>")
    pkg = make(body=sdt + para("real body", style="BodyText"))
    block = next(b for b in walk(pkg) if b.context.is_placeholder)
    assert block.text == "[Click to enter title]"
    assert "[Click to enter title]" not in document_text(pkg)


# -- normalization -------------------------------------------------------


def test_zero_width_and_directional_characters_are_stripped():
    """Invisible Cf characters otherwise defeat an exact boilerplate match."""
    assert match_key("A​B") == "AB"
    assert match_key("﻿A") == "A"
    assert match_key("A‎B‏") == "AB"


def test_match_key_is_tolerant_but_exact_key_is_not():
    """Find the block tolerantly; enforce the wording strictly."""
    em = "Approved for public release—distribution unlimited."
    hyphen = "Approved for public release-distribution unlimited."
    assert match_key(em) == match_key(hyphen)
    assert exact_key(em) != exact_key(hyphen)


def test_match_key_preserves_case():
    assert match_key("Lincoln Laboratory") == "Lincoln Laboratory"


def test_nested_table_text_is_emitted_exactly_once():
    inner = f"<w:tbl><w:tr><w:tc>{para('deep')}</w:tc></w:tr></w:tbl>"
    outer = f"<w:tbl><w:tr><w:tc>{inner}</w:tc></w:tr></w:tbl>"
    assert document_text(make(body=outer)).count("deep") == 1
