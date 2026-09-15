"""How a learned value is written down, and read back.

Values are encoded as the strings an author would write -- ``1in``, ``11pt``,
``1.15x`` -- rather than as raw OOXML integers. That choice is driven by
`overrides.yaml`, which is the one generated-adjacent file a human edits by
hand: a pin reading ``"/page/primary/margins/left": 1.25in`` is reviewable in a
pull request, and ``1800`` is not.

Decoding a string back to a typed value therefore needs to know what kind of
measurement it is, because ``12pt`` is a Length under `space_after` and a
FontSize under `size`. That knowledge is this module's other job: the leaf of
a pointer determines its type, which also means an override naming a property
we do not know about can be rejected with a useful message instead of being
silently stored as a string and silently ignored for ever after.
"""

from __future__ import annotations

from typing import Any

from ..oox.values import FontSize, HalfPoints, Length, LineSpacing
from ..util.pointer import leaf

SCHEMA_VERSION = 1

# Leaf name -> value type. Keyed on the leaf rather than the whole pointer so
# that a new section of the profile inherits the right types for free.
LENGTH_LEAVES = frozenset({
    "space_before", "space_after", "char_spacing",
    "indent_left", "indent_right", "indent_first_line", "indent_hanging",
    "top", "right", "bottom", "left", "header", "footer", "gutter",
    "width", "height", "content_width", "default_tab_stop",
})
FONT_SIZE_LEAVES = frozenset({"size", "size_cs"})
HALF_POINT_LEAVES = frozenset({"position"})
LINE_SPACING_LEAVES = frozenset({"line_spacing"})

# `header` and `footer` are Lengths only under /margins; everywhere else they
# are structural tokens that never reach the encoder as a measurement. If that
# ever stops being true, the pointer prefix has to join the key.
_AMBIGUOUS = frozenset({"header", "footer"})


# Values we compute rather than observe. They belong in the profile -- text
# width is what an image is scaled to fit -- but they must never be pinned or
# linted independently: content_width IS page width minus the margins, so a
# separate rule for it either says nothing new or contradicts the margin rule
# the moment someone changes a margin.
DERIVED_LEAVES = frozenset({"content_width"})


def is_derived(pointer: str) -> bool:
    return leaf(pointer) in DERIVED_LEAVES


def kind_of(pointer: str) -> str:
    name = leaf(pointer)
    if name in LENGTH_LEAVES:
        if name in _AMBIGUOUS and "/margins/" not in pointer:
            return "native"
        return "length"
    if name in FONT_SIZE_LEAVES:
        return "font_size"
    if name in HALF_POINT_LEAVES:
        return "half_points"
    if name in LINE_SPACING_LEAVES:
        return "line_spacing"
    return "native"


def _fmt(number: float) -> str:
    """Shortest exact decimal -- 1 not 1.0, 1.25 not 1.250000."""
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def encode(value: Any, pointer: str = "") -> Any:
    """Typed value -> something JSON and YAML can hold, readably."""
    if isinstance(value, LineSpacing):
        if value.rule == "auto":
            return f"{_fmt(value.raw / 240)}x"
        return f"{value.rule} {_fmt(value.raw / 20)}pt"
    if isinstance(value, FontSize):          # also HalfPoints, its alias
        return f"{_fmt(value.half_points / 2)}pt"
    if isinstance(value, Length):
        # Inches for anything page-sized and expressible in hundredths of an
        # inch (margins, indents), points for the small stuff (spacing). Both
        # round-trip exactly, and each matches the unit Word's own dialog
        # shows for that property, which is what makes a pin reviewable.
        twips = abs(value.twips)
        if twips >= 720 and (twips * 100) % 1440 == 0:
            return f"{_fmt(value.twips / 1440)}in"
        return f"{_fmt(value.twips / 20)}pt"
    if isinstance(value, (list, tuple)):
        return [encode(v, pointer) for v in value]
    return value


def decode(value: Any, pointer: str) -> Any:
    """The inverse, using the pointer to disambiguate ``12pt``."""
    kind = kind_of(pointer)
    if value is None or kind == "native":
        return value
    if kind == "line_spacing":
        return _decode_line_spacing(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A bare number is the OOXML native unit for that property.
        return (Length(int(value)) if kind == "length"
                else FontSize(int(value)) if kind == "font_size"
                else HalfPoints(int(value)))
    if not isinstance(value, str):
        return value
    if kind == "length":
        return Length.parse(value)
    if kind == "font_size":
        return FontSize.parse(value)
    return HalfPoints.parse(value)


def _decode_line_spacing(value: Any) -> LineSpacing | None:
    if isinstance(value, LineSpacing):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return LineSpacing("auto", int(round(float(value) * 240)))
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("x"):
        try:
            return LineSpacing("auto", int(round(float(text[:-1]) * 240)))
        except ValueError:
            return None
    rule, _, rest = text.partition(" ")
    if rule in ("exact", "atLeast") and (length := Length.parse(rest.strip())):
        return LineSpacing(rule, length.twips)  # type: ignore[arg-type]
    return None


def round_trips(value: Any, pointer: str) -> bool:
    """Does encode/decode preserve this value exactly?

    Used by the profile writer as an assertion rather than a hope: a value
    that does not survive its own serialization must not be written, because
    the next run would read back something the corpus never said.
    """
    return decode(encode(value, pointer), pointer) == value
