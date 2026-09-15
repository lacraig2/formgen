"""Numbering: the three-deep indirection, and what a paragraph resolves to."""

from __future__ import annotations

import pytest
from lxml import etree

from fixtures import build
from formgen.oox.numbering import Numbering, canonical_bullet
from formgen.oox.props import ParaProps
from formgen.oox.numbering import numbering_of
from formgen.oox.styles import StyleGraph
from formgen.opc.ns import RT, qn

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def numbering_xml(body: str) -> str:
    return f'<w:numbering {W}>{body}</w:numbering>'


def parse(body: str, styles: StyleGraph | None = None) -> Numbering:
    return Numbering.parse(etree.fromstring(numbering_xml(body).encode()), styles)


def lvl(ilvl: int, fmt: str = "decimal", text: str = "%1.", **attrs: str) -> str:
    extra = "".join(f'<w:{k} w:val="{v}"/>' for k, v in attrs.items())
    return (
        f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="1"/>'
        f'<w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text}"/>'
        f'<w:lvlJc w:val="left"/>{extra}'
        f'<w:pPr><w:ind w:left="{720 * (ilvl + 1)}" w:hanging="360"/></w:pPr>'
        f"</w:lvl>"
    )


# -- the happy path -------------------------------------------------------


def test_fixture_numbering_resolves_both_instances():
    pkg = build.make()
    num = Numbering.parse(pkg.element(pkg.related(RT["numbering"])))
    assert len(num) == 2
    bullet = num.level(1, 0)
    assert bullet is not None and bullet.is_bullet
    decimal = num.level(2, 0)
    assert decimal is not None and decimal.num_fmt == "decimal"
    assert decimal.lvl_text == "%1."
    assert num.level(2, 1).num_fmt == "lowerLetter"


def test_level_indents_come_from_the_levels_own_ppr():
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + lvl(1) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    assert num.level(1, 0).indent_left.twips == 720
    assert num.level(1, 1).indent_left.twips == 1440
    assert num.level(1, 0).indent_hanging.twips == 360


def test_missing_level_is_none_not_an_exception():
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    assert num.level(1, 5) is None
    assert num.level(99, 0) is None


# -- numId 0 and other absences ------------------------------------------


def test_numid_zero_is_explicitly_unnumbered_not_a_lookup_miss():
    """w:numId="0" REMOVES numbering the paragraph style applied."""
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    assert num.resolve(0, 0) is None
    assert num.resolve(None, None) is None
    ref = num.resolve(1, 0)
    assert ref is not None and ref.ilvl == 0


def test_dangling_instance_resolves_but_is_flagged_unresolved():
    num = parse('<w:num w:numId="7"><w:abstractNumId w:val="4"/></w:num>')
    ref = num.resolve(7, 0)
    assert ref is not None
    assert ref.level is None and ref.resolved is False
    assert num.dangling_instances() == [7]


def test_absent_ilvl_defaults_to_zero():
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + lvl(1) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    assert num.resolve(1, None).ilvl == 0


# -- overrides ------------------------------------------------------------


def test_start_override_changes_only_the_start_value():
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/>'
        '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride>'
        "</w:num>"
    )
    level = num.level(1, 0)
    assert level.start == 5
    assert level.num_fmt == "decimal"           # everything else survives
    assert level.indent_left.twips == 720


def test_full_lvl_override_replaces_rather_than_merges():
    """A w:lvlOverride carrying a w:lvl is a replacement, not a patch."""
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0, indent="x") + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/>'
        f'<w:lvlOverride w:ilvl="0">{lvl(0, "upperRoman", "%1)")}</w:lvlOverride>'
        "</w:num>"
    )
    level = num.level(1, 0)
    assert level.num_fmt == "upperRoman"
    assert level.lvl_text == "%1)"


def test_two_instances_of_one_abstract_are_independent():
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="0"/>'
        '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="9"/></w:lvlOverride>'
        "</w:num>"
    )
    assert num.level(1, 0).start == 1
    assert num.level(2, 0).start == 9


# -- numStyleLink ---------------------------------------------------------


def _styles_with(*extra: str) -> StyleGraph:
    xml = build.styles_xml(list(build.DEFAULT_STYLES) + list(extra))
    return StyleGraph.parse(etree.fromstring(xml.encode()))


def test_num_style_link_follows_through_a_style_to_the_real_levels():
    """An abstract with numStyleLink is a pointer, not a definition."""
    styles = _styles_with(
        build.style(
            "ListBullet", "List Bullet",
            ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr>',
        )
    )
    num = parse(
        '<w:abstractNum w:abstractNumId="0">'
        '<w:numStyleLink w:val="ListBullet"/></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1"><w:styleLink w:val="ListBullet"/>'
        + lvl(0, "bullet", "•")
        + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>',
        styles,
    )
    level = num.level(1, 0)
    assert level is not None and level.is_bullet
    assert num.abstract_for(1).abstract_id == 1


def test_num_style_link_cycle_terminates():
    styles = _styles_with(
        build.style("A", "A", ppr='<w:numPr><w:numId w:val="2"/></w:numPr>'),
        build.style("B", "B", ppr='<w:numPr><w:numId w:val="1"/></w:numPr>'),
    )
    num = parse(
        '<w:abstractNum w:abstractNumId="0"><w:numStyleLink w:val="A"/></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1"><w:numStyleLink w:val="B"/></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>',
        styles,
    )
    # It must terminate rather than hang; the definition is then unusable,
    # which is exactly what Word renders (nothing), so it reports as dangling.
    assert num.abstract_for(1) is None
    assert num.level(1, 0) is None
    assert num.dangling_instances() == [1, 2]


def test_num_style_link_without_a_style_graph_degrades_quietly():
    num = parse(
        '<w:abstractNum w:abstractNumId="0">'
        '<w:numStyleLink w:val="ListBullet"/></w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    assert num.level(1, 0) is None


# -- style-linked levels (numbered headings) ------------------------------


def test_a_level_that_names_a_style_numbers_paragraphs_of_that_style():
    """Numbered headings carry no w:numPr; the LEVEL finds the paragraph."""
    styles = _styles_with()
    num = parse(
        '<w:abstractNum w:abstractNumId="0">'
        + lvl(0, "decimal", "%1", pStyle="Heading1")
        + lvl(1, "decimal", "%1.%2", pStyle="Heading2")
        + "</w:abstractNum>"
        '<w:num w:numId="3"><w:abstractNumId w:val="0"/></w:num>',
        styles,
    )
    assert num.style_levels() == {"Heading1": (3, 0), "Heading2": (3, 1)}
    ref = num.resolve(None, None, "Heading2")
    assert ref is not None
    assert (ref.num_id, ref.ilvl, ref.source) == (3, 1, "level-pstyle")
    assert ref.level.lvl_text == "%1.%2"


def test_style_linked_level_beats_a_defaulted_ilvl_of_zero():
    styles = _styles_with()
    num = parse(
        '<w:abstractNum w:abstractNumId="0">'
        + lvl(0, "decimal", "%1", pStyle="Heading1")
        + lvl(1, "decimal", "%1.%2", pStyle="Heading2")
        + "</w:abstractNum>"
        '<w:num w:numId="3"><w:abstractNumId w:val="0"/></w:num>',
        styles,
    )
    # numId present, ilvl absent: without the pStyle rule this is level 0,
    # which would render every Heading 2 as "1".
    assert num.resolve(3, None, "Heading2").ilvl == 1


# -- bullets --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,font,expected",
    [
        ("", "Symbol", "•"),
        ("", "Wingdings", "▪"),
        ("o", "Courier New", "o"),
        ("•", "Arial", "•"),
        ("", "Arial", ""),    # not a Symbol bullet: leave it alone
        ("-", None, "-"),
    ],
)
def test_bullet_glyphs_canonicalise_per_font(text, font, expected):
    assert canonical_bullet(text, font) == expected


def test_the_same_bullet_in_two_fonts_gets_one_signature():
    """Otherwise a corpus looks as though its authors disagreed about bullets."""
    symbol = parse(
        '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0">'
        '<w:numFmt w:val="bullet"/><w:lvlText w:val=""/>'
        '<w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr>'
        "</w:lvl></w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    unicode_ = parse(
        '<w:abstractNum w:abstractNumId="9"><w:lvl w:ilvl="0">'
        '<w:numFmt w:val="bullet"/><w:lvlText w:val="•"/>'
        "</w:lvl></w:abstractNum>"
        '<w:num w:numId="4"><w:abstractNumId w:val="9"/></w:num>'
    )
    assert symbol.shape(1) == unicode_.shape(4)


# -- cross-document identity ---------------------------------------------


def test_signature_ignores_document_local_ids():
    a = parse(
        '<w:abstractNum w:abstractNumId="0"><w:nsid w:val="AAAA1111"/>'
        + lvl(0) + lvl(1, "lowerLetter", "%2)")
        + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    b = parse(
        '<w:abstractNum w:abstractNumId="17"><w:nsid w:val="BBBB2222"/>'
        + lvl(0) + lvl(1, "lowerLetter", "%2)")
        + "</w:abstractNum>"
        '<w:num w:numId="42"><w:abstractNumId w:val="17"/></w:num>'
    )
    assert a.signature(1) == b.signature(42)


def test_signature_distinguishes_different_indents():
    base = '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0">' \
           '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/>' \
           '<w:pPr><w:ind w:left="{left}" w:hanging="360"/></w:pPr></w:lvl>' \
           "</w:abstractNum>" \
           '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    a = parse(base.format(left=720))
    b = parse(base.format(left=1080))
    assert a.signature(1) != b.signature(1)
    assert a.shape(1) == b.shape(1)      # same KIND of list, different geometry


def test_tentative_levels_are_excluded_from_the_signature():
    """Word pads hybridMultilevel out to nine levels the author never chose."""
    real = lvl(0) + lvl(1, "lowerLetter", "%2)")
    padded = real + '<w:lvl w:ilvl="2" w:tentative="1"><w:numFmt w:val="lowerRoman"/>' \
                    '<w:lvlText w:val="%3."/></w:lvl>'
    a = parse(
        f'<w:abstractNum w:abstractNumId="0">{real}</w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    b = parse(
        f'<w:abstractNum w:abstractNumId="0">{padded}</w:abstractNum>'
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    )
    assert a.signature(1) == b.signature(1)
    assert b.signature(1, include_tentative=True) != a.signature(1)


def test_duplicate_nsid_is_reported():
    """Word merges two lists that share an nsid -- a classic generator bug."""
    num = parse(
        '<w:abstractNum w:abstractNumId="0"><w:nsid w:val="0A1B2C3D"/></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="1"><w:nsid w:val="0a1b2c3d"/></w:abstractNum>'
        '<w:abstractNum w:abstractNumId="2"><w:nsid w:val="FFFFFFFF"/></w:abstractNum>'
    )
    assert num.duplicate_nsids() == {"0a1b2c3d": [0, 1]}


# -- the paragraph-level entry point --------------------------------------


def test_numbering_of_reads_numbering_off_the_paragraph_style():
    styles = _styles_with(
        build.style(
            "ListNumber", "List Number",
            ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>',
        )
    )
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>',
        styles,
    )
    ref = numbering_of(styles, num, "ListNumber", ParaProps())
    assert ref is not None and ref.num_id == 1


def test_direct_numid_zero_cancels_the_styles_numbering():
    styles = _styles_with(
        build.style(
            "ListNumber", "List Number",
            ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>',
        )
    )
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>',
        styles,
    )
    assert numbering_of(styles, num, "ListNumber", ParaProps(num_id=0)) is None


def test_direct_ilvl_demotes_within_the_styles_list():
    styles = _styles_with(
        build.style(
            "ListNumber", "List Number",
            ppr='<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>',
        )
    )
    num = parse(
        '<w:abstractNum w:abstractNumId="0">' + lvl(0) + lvl(1, "lowerLetter", "%2)")
        + "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>',
        styles,
    )
    ref = numbering_of(styles, num, "ListNumber", ParaProps(num_level=1))
    assert (ref.num_id, ref.ilvl) == (1, 1)
    assert ref.level.num_fmt == "lowerLetter"


def test_numbering_of_with_no_numbering_part_is_none():
    styles = _styles_with()
    assert numbering_of(styles, None, "BodyText", ParaProps(num_id=1)) is None


# -- performance ----------------------------------------------------------


def test_resolving_every_paragraph_does_not_rescan_every_list():
    """style_levels() is consulted per paragraph; rebuilding it is O(n*lists)."""
    abstracts = "".join(
        f'<w:abstractNum w:abstractNumId="{i}">' + lvl(0) + "</w:abstractNum>"
        for i in range(40)
    )
    instances = "".join(
        f'<w:num w:numId="{i + 1}"><w:abstractNumId w:val="{i}"/></w:num>'
        for i in range(40)
    )
    num = parse(abstracts + instances, _styles_with())

    import time

    start = time.perf_counter()
    for _ in range(5000):
        num.resolve(None, None, "BodyText")
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"{elapsed:.2f}s for 5000 resolutions -- cache lost?"
