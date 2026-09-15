"""Typed measurement values.

Units in OOXML are a minefield, so they get real types rather than bare numbers.
The worst offender is w:spacing/@w:line, whose unit depends on a sibling
attribute: with w:lineRule="auto" it is 240ths of a line, with "exact"/"atLeast"
it is twips. Treating those as the same number is a 10x error, so LineSpacing
keeps the rule attached to the value and refuses to compare across rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ST_TwipsMeasure and ST_HpsMeasure both admit ST_UniversalMeasure -- a number
# with a unit suffix. Word never writes one, but Aspose, docx4j and several
# HTML converters do, and parsing "0.5in" as "not a number" silently loses a
# page size or a margin.
_UNIVERSAL = re.compile(r"^\s*([+-]?[0-9]*\.?[0-9]+)\s*(mm|cm|in|pt|pc|pi)\s*$", re.I)

TWIPS_PER_INCH = 1440
TWIPS_PER_POINT = 20
TWIPS_PER_CM = 566.929133858
EMU_PER_TWIP = 635

_UNIT_TWIPS = {
    "in": 1440.0, "pt": 20.0, "pc": 240.0, "pi": 240.0,
    "mm": 1440.0 / 25.4, "cm": 1440.0 / 2.54,
}


def _universal(raw: str, per_point: float) -> float | None:
    """Convert a unit-suffixed measure to the caller's base unit.

    `per_point` is how many base units make one point: 20 for twips, 2 for
    half-points.
    """
    match = _UNIVERSAL.match(raw)
    if not match:
        return None
    number, unit = float(match.group(1)), match.group(2).lower()
    return number * _UNIT_TWIPS[unit.lower()] / 20.0 * per_point


@dataclass(frozen=True, order=True)
class Length:
    """A length in twips (twentieths of a point), OOXML's native unit."""

    twips: int

    @classmethod
    def pt(cls, points: float) -> Length:
        return cls(round(points * TWIPS_PER_POINT))

    @classmethod
    def inches(cls, value: float) -> Length:
        return cls(round(value * TWIPS_PER_INCH))

    @classmethod
    def cm(cls, value: float) -> Length:
        return cls(round(value * TWIPS_PER_CM))

    @classmethod
    def emu(cls, value: int) -> Length:
        return cls(round(value / EMU_PER_TWIP))

    @classmethod
    def parse(cls, raw: str | None) -> Length | None:
        """Parse a twips integer, or a unit-suffixed universal measure."""
        if raw is None:
            return None
        try:
            return cls(int(round(float(raw))))
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            value = _universal(raw, per_point=20.0)
        except (TypeError, ValueError, OverflowError):
            return None
        return None if value is None else cls(int(round(value)))

    @property
    def points(self) -> float:
        return self.twips / TWIPS_PER_POINT

    @property
    def inch(self) -> float:
        return self.twips / TWIPS_PER_INCH

    @property
    def to_emu(self) -> int:
        return self.twips * EMU_PER_TWIP

    def __str__(self) -> str:
        p = self.points
        return f"{p:g}pt" if p == int(p) else f"{p:.2f}pt"


@dataclass(frozen=True, order=True)
class FontSize:
    """A font size. OOXML stores half-points in w:sz, so 22 means 11pt."""

    half_points: int

    @classmethod
    def pt(cls, points: float) -> FontSize:
        return cls(round(points * 2))

    @classmethod
    def parse(cls, raw: str | None) -> FontSize | None:
        """Parse a half-point integer, or a unit-suffixed universal measure."""
        if raw is None:
            return None
        try:
            return cls(int(round(float(raw))))
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            value = _universal(raw, per_point=2.0)
        except (TypeError, ValueError, OverflowError):
            return None
        return None if value is None else cls(int(round(value)))

    @property
    def points(self) -> float:
        return self.half_points / 2

    def __str__(self) -> str:
        p = self.points
        return f"{p:g}pt"


LineRule = Literal["auto", "exact", "atLeast"]


@dataclass(frozen=True)
class LineSpacing:
    """w:spacing/@w:line plus its @w:lineRule.

    The rule is part of the value. `auto` carries a multiple (1.0 = single);
    `exact` and `atLeast` carry an absolute Length. Comparing one to the other
    is meaningless, so `same_kind_as` gates comparison explicitly.
    """

    rule: LineRule
    raw: int

    @classmethod
    def parse(cls, line: str | None, rule: str | None) -> LineSpacing | None:
        if line is None:
            return None
        try:
            value = int(round(float(line)))
        except (TypeError, ValueError, OverflowError):
            return None
        r: LineRule = rule if rule in ("auto", "exact", "atLeast") else "auto"
        return cls(r, value)

    @property
    def multiple(self) -> float | None:
        """Line multiple, for rule='auto' only. 240 twentieths == single."""
        return self.raw / 240 if self.rule == "auto" else None

    @property
    def length(self) -> Length | None:
        """Absolute height, for rule='exact'/'atLeast' only."""
        return None if self.rule == "auto" else Length(self.raw)

    def same_kind_as(self, other: LineSpacing) -> bool:
        return self.rule == other.rule

    def __str__(self) -> str:
        if self.rule == "auto":
            return f"{self.multiple:g}x"
        return f"{self.rule} {Length(self.raw)}"


# w:position and w:szCs are ST_SignedHpsMeasure / ST_HpsMeasure: HALF-POINTS,
# not twips. Naming the alias keeps the unit visible at the point of use.
HalfPoints = FontSize


def half_point_str(size: FontSize) -> str:
    return str(size.half_points)


def parse_on_off(raw: str | None, *, present: bool) -> bool | None:
    """Interpret an OOXML on/off attribute.

    Returns None when the element is absent, True/False otherwise. A bare
    element (<w:b/>) means on; w:val of 0/false/off means off.
    """
    if not present:
        return None
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "off")
