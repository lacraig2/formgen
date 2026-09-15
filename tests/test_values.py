"""Typed measurements: the units OOXML gets wrong by looking right."""

from __future__ import annotations

import pytest
from lxml import etree

from formgen.oox.props import RunProps
from formgen.oox.values import FontSize, HalfPoints, Length, LineSpacing

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def rpr(inner: str) -> etree._Element:
    return etree.fromstring(f"<w:rPr {W}>{inner}</w:rPr>".encode())


# -- universal measures ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,twips",
    [
        ("1440", 1440),
        ("1in", 1440),
        ("0.5in", 720),
        ("72pt", 1440),
        ("-0.5in", -720),
        ("25.4mm", 1440),
        ("2.54cm", 1440),
        ("6pc", 1440),
        (" 1in ", 1440),
        ("1IN", 1440),
    ],
)
def test_lengths_accept_unit_suffixed_measures(raw, twips):
    """Word never writes these; Aspose, docx4j and HTML converters do."""
    assert Length.parse(raw) == Length(twips)


@pytest.mark.parametrize("raw", ["", "abc", "1furlong", "in", "1 2in", None, "inf"])
def test_nonsense_measures_are_none_not_an_exception(raw):
    assert Length.parse(raw) is None


def test_infinity_does_not_survive_as_a_length():
    """float('inf') parses fine; int(round(inf)) is where it blows up."""
    assert Length.parse("Infinity") is None
    assert FontSize.parse("-inf") is None


def test_font_sizes_accept_point_suffixed_measures():
    assert FontSize.parse("22") == FontSize.pt(11)
    assert FontSize.parse("11pt") == FontSize.pt(11)
    assert FontSize.parse("0.5in") == FontSize.pt(36)


# -- w:position is half-points, not twips ---------------------------------


def test_w_position_is_read_as_half_points():
    """Reading it as twips turns a 3pt raise into 0.15pt -- invisible."""
    props = RunProps.parse(rpr('<w:position w:val="6"/>'))
    assert props.position == HalfPoints(6)
    assert props.position.points == 3.0


def test_w_spacing_in_a_run_really_is_twips():
    """The neighbouring property with the other unit, so the pair is pinned."""
    props = RunProps.parse(rpr('<w:spacing w:val="20"/>'))
    assert props.char_spacing == Length(20)
    assert props.char_spacing.points == 1.0


def test_negative_position_is_a_subscript_offset():
    props = RunProps.parse(rpr('<w:position w:val="-4"/>'))
    assert props.position.points == -2.0


# -- line spacing keeps its rule ------------------------------------------


def test_line_spacing_of_240_means_two_different_things():
    auto = LineSpacing.parse("240", "auto")
    exact = LineSpacing.parse("240", "exact")
    assert auto.multiple == 1.0 and auto.length is None
    assert exact.length == Length(240) and exact.multiple is None
    assert not auto.same_kind_as(exact)


def test_an_unknown_line_rule_falls_back_to_auto():
    assert LineSpacing.parse("360", "bogus").rule == "auto"
    assert LineSpacing.parse("360", None).rule == "auto"
