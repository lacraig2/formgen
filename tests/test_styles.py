"""Style graph, inheritance resolution, and the OOXML traps that surround it."""

from __future__ import annotations

import pytest
from lxml import etree

from fixtures.build import make, style, styles_xml
from formgen.opc.ns import RT, qn
from formgen.oox.props import ParaProps, RunProps, finalize, fold, resolve_toggle
from formgen.oox.styles import StyleGraph, normalize_style_name
from formgen.oox.theme import Theme
from formgen.oox.values import FontSize, Length, LineSpacing


def graph(styles=None, default_sz=22) -> StyleGraph:
    pkg = make(styles=styles, default_sz=default_sz)
    theme = Theme.parse(pkg.element(pkg.related(RT["theme"])))
    return StyleGraph.parse(pkg.element(pkg.related(RT["styles"])), theme)


def parse_styles(xml: str) -> StyleGraph:
    return StyleGraph.parse(etree.fromstring(xml.encode()))


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def rpr(inner: str) -> RunProps:
    """Parse a <w:rPr> fragment, e.g. rpr("<w:b/>")."""
    return RunProps.parse(etree.fromstring(f'<w:rPr xmlns:w="{W_NS}">{inner}</w:rPr>'.encode()))


def ppr(inner: str) -> ParaProps:
    """Parse a <w:pPr> fragment."""
    return ParaProps.parse(etree.fromstring(f'<w:pPr xmlns:w="{W_NS}">{inner}</w:pPr>'.encode()))


# -- trap 1: docDefaults is the inheritance root, not Normal ------------


def test_docdefaults_supplies_the_body_size_when_normal_is_empty():
    """The #1 source of false 'these documents disagree' reports."""
    sg = graph(default_sz=24)  # 12pt in docDefaults, Normal carries nothing
    assert sg.effective("Normal").run.size == FontSize.pt(12)
    assert sg.effective("BodyText").run.size == FontSize.pt(12)


def test_a_style_overrides_docdefaults():
    sg = graph(default_sz=22)
    assert sg.effective("Heading1").run.size == FontSize.pt(16)
    assert sg.effective("BodyText").run.size == FontSize.pt(11)


def test_docdefaults_paragraph_props_reach_every_style():
    sg = graph()
    # docDefaults sets spacing after=160; BodyText overrides it to 120.
    assert sg.effective("Normal").para.space_after == Length(160)
    assert sg.effective("BodyText").para.space_after == Length(120)


# -- inheritance chains --------------------------------------------------


def test_chain_is_root_first_and_the_leaf_wins():
    sg = parse_styles(styles_xml([
        style("A", "A", based_on=None, rpr='<w:sz w:val="20"/><w:color w:val="FF0000"/>'),
        style("B", "B", based_on="A", rpr='<w:sz w:val="40"/>'),
    ]))
    eff = sg.effective("B")
    assert eff.chain == ["A", "B"]
    assert eff.run.size == FontSize.pt(20)      # leaf wins
    assert eff.run.color == "FF0000"            # inherited from the root


def test_dangling_based_on_falls_back_to_docdefaults_not_normal():
    sg = parse_styles(styles_xml([
        style("Normal2", "Normal2", based_on=None, rpr='<w:sz w:val="96"/>'),
        style("Orphan", "Orphan", based_on="DoesNotExist"),
    ]))
    assert sg.dangling_based_on() == [("Orphan", "DoesNotExist")]
    # docDefaults is 11pt; it must NOT pick up Normal2's 48pt.
    assert sg.effective("Orphan").run.size == FontSize.pt(11)


def test_cyclic_based_on_terminates():
    """Word tolerates some cyclic basedOn graphs, so we must not hang."""
    sg = parse_styles(styles_xml([
        style("X", "X", based_on="Y"),
        style("Y", "Y", based_on="X"),
    ]))
    assert sg.has_cycle("X")
    assert len(sg.ancestry("X")) <= 2
    sg.effective("X")  # must return


def test_self_referential_based_on_terminates():
    sg = parse_styles(styles_xml([style("S", "S", based_on="S")]))
    assert sg.ancestry("S") == ["S"]


# -- trap 2: identity keys on w:name, not w:styleId ----------------------


def test_localised_style_id_still_matches_by_english_name():
    """Built-ins keep an English w:name while w:styleId may be localised."""
    sg = parse_styles(styles_xml([
        style("berschrift1", "heading 1", outline=0, rpr='<w:sz w:val="32"/>'),
    ]))
    assert sg.by_name("Heading 1").style_id == "berschrift1"
    assert sg.outline_level_of("berschrift1") == 0


def test_style_name_lookup_is_case_and_whitespace_insensitive():
    sg = graph()
    for spelling in ["Body Text", "body text", "  BODY   TEXT  "]:
        assert sg.by_name(spelling).style_id == "BodyText"


def test_linked_character_half_resolves_to_its_paragraph_style():
    """Heading1 <-> Heading1Char, aliased via w:link rather than by suffix."""
    sg = parse_styles(styles_xml([
        style("Heading1", "heading 1", outline=0, link="Heading1Char",
              rpr='<w:sz w:val="32"/>'),
        style("Heading1Char", "heading 1 Char", type="character",
              based_on=None, link="Heading1", rpr='<w:sz w:val="32"/>'),
    ]))
    assert sg.by_name("heading 1 Char").style_id == "Heading1"
    assert normalize_style_name("Heading 1 Char") != normalize_style_name("heading 1")


def test_comma_separated_aliases_all_resolve():
    sg = parse_styles(styles_xml([style("S1", "Quote,Block Quote,BQ")]))
    assert sg.by_id("S1").name == "Quote"
    for alias in ["Quote", "Block Quote", "BQ"]:
        assert sg.by_name(alias).style_id == "S1"


def test_unknown_name_returns_none():
    assert graph().by_name("No Such Style") is None


# -- trap 3: theme font indirection --------------------------------------


def test_theme_reference_beats_an_explicit_typeface():
    """Setting w:ascii without clearing w:asciiTheme is a silent no-op."""
    theme = Theme(fonts={"minorHAnsi": "Calibri"})
    assert theme.resolve_font("Times New Roman", "minorHAnsi") == "Calibri"
    assert theme.resolve_font("Times New Roman", None) == "Times New Roman"


def test_style_font_resolves_through_the_theme():
    sg = graph()
    assert sg.effective("BodyText").font == "Calibri"       # via minorHAnsi
    assert sg.effective("BodyText").run.font_ascii_theme == "minorHAnsi"


def test_theme_colours_resolve_including_the_dk_lt_naming_mismatch():
    sg = graph()
    theme = sg.theme
    assert theme.color("accent1") == "4472C4"
    assert theme.color("text1") == "000000"        # text1 -> dk1
    assert theme.color("background1") == "FFFFFF"  # background1 -> lt1
    assert theme.color("hyperlink") == "0563C1"    # hyperlink -> hlink
    assert theme.color(None) is None


# -- trap 4: toggle properties XOR across levels -------------------------


def test_direct_bold_on_a_bold_style_stays_bold():
    """Direct formatting OVERRIDES; it does not XOR with the style hierarchy.

    MS-OI29500 17.7.3(a) conditions the toggle rule on the property not being
    set in direct formatting. Word honours what the user pressed the button
    for, and a bare <w:b/> is what most generators emit.
    """
    sg = parse_styles(styles_xml([style("B", "B", based_on=None, rpr="<w:b/>")]))
    assert sg.effective_for_run("B", None, rpr("<w:b/>")).bold is True
    assert sg.effective_for_run("B", None, rpr('<w:b w:val="0"/>')).bold is False
    assert sg.effective_for_run("B", None, RunProps()).bold is True


def test_explicit_off_beats_an_inherited_on():
    assert resolve_toggle([None, True, False]) is False


@pytest.mark.parametrize("depth", [2, 3, 4, 5])
def test_a_based_on_chain_is_plain_inheritance_not_xor(depth):
    """MS-OI29500 17.7.3(c): a basedOn chain inherits, it does not toggle.

    Parametrised deliberately: an XOR bug passes at odd depths and fails at
    even ones, so a single fixed-depth test is a coin flip.
    """
    styles = [style("S0", "S0", based_on=None, rpr="<w:b/>")]
    styles += [style(f"S{i}", f"S{i}", based_on=f"S{i-1}", rpr="<w:b/>")
               for i in range(1, depth)]
    sg = parse_styles(styles_xml(styles))
    assert sg.effective(f"S{depth - 1}").run.bold is True


def test_toggles_xor_only_at_the_character_style_level():
    """Word resets on paragraph/table styles and XORs only on character styles.

    [MS-OI29500] 2.1.257: "Word resets the value of the toggle property to the
    value specified by the paragraph style if a value is present". No such
    deviation is recorded for character styles, so the XOR survives there.
    """
    sg = parse_styles(styles_xml([
        style("P", "P", based_on=None, rpr="<w:b/>"),
        style("C", "C", type="character", based_on=None, rpr="<w:b/>"),
    ]))
    assert sg.effective_for_run("P", "C", RunProps()).bold is False


def test_document_defaults_supply_unspecified_levels():
    """MS-OI29500 17.7.3(b): an unspecified level takes the document default."""
    sg = parse_styles(
        styles_xml([style("E", "E", based_on=None)]).replace(
            "<w:lang w:val=\"en-US\"/>", "<w:lang w:val=\"en-US\"/><w:b/>")
    )
    # An empty style must not cancel a document-default toggle.
    assert sg.effective("E").run.bold is True
    assert sg.effective_for_run("E", None, RunProps()).bold is True
    # ...but direct formatting still wins.
    assert sg.effective_for_run("E", None, rpr('<w:b w:val="0"/>')).bold is False


def test_an_empty_style_level_is_still_tri_state():
    """fold() must not flatten toggles to bool, or empty levels inject False."""
    assert fold([RunProps()], RunProps).bold is None
    assert finalize(RunProps(bold=True),
                    [("paragraph", fold([RunProps()], RunProps))],
                    RunProps(), RunProps).bold is True


def test_dstrike_is_not_a_toggle_property():
    from formgen.oox.props import TOGGLE_FIELDS

    assert "dstrike" not in TOGGLE_FIELDS
    assert "bold" in TOGGLE_FIELDS and "caps" in TOGGLE_FIELDS


# -- trap 5: line spacing units depend on lineRule -----------------------


def test_line_spacing_keeps_its_rule_and_refuses_cross_kind_comparison():
    auto = LineSpacing.parse("240", "auto")
    exact = LineSpacing.parse("240", "exact")
    assert auto.multiple == 1.0 and auto.length is None
    assert exact.length == Length(240) and exact.multiple is None
    assert not auto.same_kind_as(exact)
    assert auto != exact           # same raw number, different meaning


def test_missing_line_rule_defaults_to_auto():
    assert LineSpacing.parse("259", None).rule == "auto"


# -- trap 11: w:ind start/end (strict) vs left/right (transitional) ------


def test_indent_reads_both_transitional_and_strict_attributes():
    transitional = ppr('<w:ind w:left="720" w:right="360"/>')
    strict = ppr('<w:ind w:start="720" w:end="360"/>')
    assert transitional.indent_left == strict.indent_left == Length(720)
    assert transitional.indent_right == strict.indent_right == Length(360)


# -- general resolution behaviour ---------------------------------------


def test_absent_level_falls_through():
    a = RunProps(size=FontSize.pt(10), color="FF0000")
    assert fold([a, RunProps()], RunProps).size == FontSize.pt(10)


def test_nearest_specified_value_wins_for_ordinary_properties():
    a = RunProps(size=FontSize.pt(10))
    b = RunProps(size=FontSize.pt(14))
    assert fold([a, b], RunProps).size == FontSize.pt(14)


def test_rfonts_explicit_and_theme_move_together_across_levels():
    """A nearer w:ascii clears an inherited w:asciiTheme rather than losing to it.

    Otherwise every "user picked a non-theme font" case -- the most common
    direct-formatting deviation there is -- resolves to the theme font.
    """
    got = finalize(
        RunProps(font_ascii_theme="minorHAnsi"),
        [("paragraph", fold([RunProps(font_ascii="Arial")], RunProps))],
        RunProps(),
        RunProps,
    )
    assert got.font_ascii == "Arial"
    assert got.font_ascii_theme is None


def test_unstyled_paragraph_inherits_the_default_paragraph_style():
    """Most paragraphs carry no w:pStyle; they are not unformatted."""
    sg = graph()
    assert sg.effective_for_para(None, ParaProps()).space_after == Length(160)


def test_style_default_attribute_honours_off():
    """ST_OnOff attributes accept 'off'; reading it as on picks the wrong default."""
    sg = parse_styles(styles_xml([
        '<w:style w:type="paragraph" w:default="off" w:styleId="NotDefault">'
        '<w:name w:val="Not Default"/></w:style>',
        '<w:style w:type="paragraph" w:default="1" w:styleId="Real">'
        '<w:name w:val="Real"/></w:style>',
    ]))
    assert sg.default_paragraph_style().style_id == "Real"


def test_last_default_in_document_order_wins():
    sg = parse_styles(styles_xml([
        style("First", "First", based_on=None) .replace('w:type="paragraph"',
             'w:type="paragraph" w:default="1"'),
        style("Last", "Last", based_on=None).replace('w:type="paragraph"',
             'w:type="paragraph" w:default="1"'),
    ]))
    assert sg.default_paragraph_style().style_id == "Last"


def test_undefined_style_is_flagged_rather_than_silently_resolved():
    """Word renders a built-in we do not have; guessing invents deviations."""
    sg = graph()
    missing = sg.effective("NoSuchStyle")
    assert missing.resolved is False
    assert sg.effective("BodyText").resolved is True


def test_char_suffix_only_aliases_a_genuine_linked_pair():
    """'Sidebar Char' must not hijack lookups for an unrelated 'Sidebar'."""
    sg = parse_styles(styles_xml([
        style("Sidebar", "Sidebar", based_on=None, ppr='<w:ind w:left="720"/>'),
        style("SidebarChar", "Sidebar Char", type="character", based_on=None,
              rpr='<w:sz w:val="16"/>'),
    ]))
    assert sg.by_name("Sidebar").style_id == "Sidebar"
    assert sg.by_name("Sidebar Char", type="character").style_id == "SidebarChar"


def test_based_on_does_not_jump_style_type():
    sg = parse_styles(styles_xml([
        style("CharBase", "Char Base", type="character", based_on=None,
              ppr='<w:ind w:left="5000"/>'),
        style("Para", "Para", based_on="CharBase"),
    ]))
    assert sg.effective_for_para("Para", ParaProps()).indent_left != Length(5000)


def test_outline_level_nine_means_body_text_not_heading_nine():
    sg = parse_styles(styles_xml([style("Body9", "Body9", outline=9)]))
    assert sg.outline_level_of("Body9") is None


def test_infinite_value_does_not_crash_the_parser():
    from formgen.oox.values import FontSize as FS

    assert Length.parse("inf") is None
    assert FS.parse("Infinity") is None


def test_paragraph_cascade_applies_direct_over_style():
    direct = ppr('<w:jc w:val="center"/>')
    sg = graph()
    assert sg.effective("BodyText").para.alignment == "both"
    assert sg.effective_for_para("BodyText", direct).alignment == "center"


def test_default_paragraph_style_is_found_by_the_default_attribute():
    assert graph().default_paragraph_style().style_id == "Normal"


def test_numbering_reference_is_parsed():
    props = ppr('<w:numPr><w:ilvl w:val="1"/><w:numId w:val="3"/></w:numPr>')
    assert props.num_id == 3 and props.num_level == 1


def test_num_id_zero_is_preserved_as_explicit_no_numbering():
    """numId=0 removes inherited numbering; it is not the same as absent."""
    assert ppr('<w:numPr><w:numId w:val="0"/></w:numPr>').num_id == 0
    assert ParaProps().num_id is None


def test_latent_styles_are_captured():
    sg = graph()
    assert "heading 1" in sg.latent


def test_paragraph_style_resets_a_document_default_toggle():
    """docDefaults bold + a paragraph style that says b=0 is NOT bold.

    An XOR-everything model returns bold here, which is the bug this test
    exists to pin.
    """
    from formgen.oox.props import finalize as fin

    assert fin(RunProps(bold=True), [("paragraph", RunProps(bold=False))],
               RunProps(), RunProps).bold is False


def test_table_and_paragraph_styles_both_reset_rather_than_cancelling():
    from formgen.oox.props import finalize as fin

    assert fin(RunProps(), [("table", RunProps(bold=True)),
                            ("paragraph", RunProps(bold=True))],
               RunProps(), RunProps).bold is True


def test_dangling_pstyle_behaves_like_an_absent_one():
    """17.3.1.27: an omitted pStyle and one naming a missing style are the same."""
    sg = graph()
    assert sg.effective_for_para("NoSuchStyle", ParaProps()).space_after == Length(160)


def test_word_cached_colour_beats_a_naive_theme_lookup():
    """<w:color w:val="595959" w:themeColor="text1" w:themeTint="A6"/> is grey."""
    from formgen.oox.theme import Theme as T

    theme = T(colors={"dk1": "000000"})
    assert theme.resolve_color("595959", "text1", tint="A6") == "595959"
    assert theme.resolve_color(None, "text1") == "000000"
    assert theme.resolve_color("auto", "text1") == "000000"


def test_word_ignored_style_ids_contribute_no_formatting():
    """Word discards child elements of NoList/DefaultParagraphFont/TableNormal."""
    sg = parse_styles(styles_xml([
        style("DefaultParagraphFont", "Default Paragraph Font",
              type="character", based_on=None, rpr='<w:sz w:val="96"/>'),
    ]))
    assert sg.effective("DefaultParagraphFont").run.size != FontSize.pt(48)
