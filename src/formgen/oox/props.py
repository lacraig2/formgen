"""Run and paragraph properties, and how they combine across levels.

Each instance describes ONE level of the cascade (document defaults, a style
chain, direct formatting). Levels are combined by :func:`resolve`, which is
where OOXML's two awkward rules live:

* ordinary properties -- the nearest level that specifies one wins;
* *toggle* properties -- ECMA-376 17.7.3, as clarified by [MS-OI29500].

The toggle rule is narrower than it first appears, and getting it wrong is the
classic OOXML formatting bug. Per [MS-OI29500] 17.7.3:

  (a) "if a toggle property is **not explicitly set in direct formatting**,
      the toggle property appears at multiple levels of the style hierarchy...
      the effective value shall be true if and only if its effective value is
      false for an even number of levels"
  (b) "if no value is encountered on a level of the style hierarchy, the
      property takes on the value specified by the document defaults"
  (c) "if a paragraph style does not set a bold property value to false, and
      the paragraph style specified by its basedOn element specifies that it
      is true, the result... sets the value of bold to true"

So, concretely:

* a basedOn chain is **plain inheritance**, not XOR -- (c). Three nested bold
  styles are bold, not "odd parity".
* XOR happens only **between** the rolled-up table / paragraph / character
  style levels -- (a).
* document defaults **short-circuit** rather than joining the XOR -- (b).
* direct formatting **overrides outright** -- (a) is conditioned on direct
  formatting being absent. Word honours what the user pressed the button for.

We still always write an explicit w:val rather than a bare element, so nothing
we emit depends on a reader implementing this correctly.

Note that w:dstrike is NOT a toggle property, despite looking like one.

https://learn.microsoft.com/en-us/openspecs/office_standards/ms-oi29500/f7130225-2368-48f3-acae-a9d278d0fb25
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

from lxml import etree

from ..opc.ns import qn
from .values import FontSize, HalfPoints, Length, LineSpacing, parse_on_off

# ECMA-376 17.7.3. w:dstrike is deliberately absent -- it is a plain on/off.
TOGGLE_FIELDS = frozenset(
    {"bold", "bold_cs", "italic", "italic_cs", "caps", "small_caps",
     "strike", "vanish", "emboss", "imprint", "outline", "shadow"}
)


def _on_off(parent: etree._Element, tag: str) -> bool | None:
    el = parent.find(qn(tag))
    return parse_on_off(el.get(qn("w:val")) if el is not None else None,
                        present=el is not None)


def _val(parent: etree._Element, tag: str) -> str | None:
    el = parent.find(qn(tag))
    return el.get(qn("w:val")) if el is not None else None


@dataclass(frozen=True)
class RunProps:
    """A w:rPr at one level of the cascade. None means 'not specified here'."""

    style_id: str | None = None          # w:rStyle -- remapped, never stripped
    font_ascii: str | None = None
    font_hansi: str | None = None
    font_cs: str | None = None
    font_ascii_theme: str | None = None  # beats font_ascii in Word's resolution
    font_hansi_theme: str | None = None
    size: FontSize | None = None
    size_cs: FontSize | None = None
    color: str | None = None
    color_theme: str | None = None
    underline: str | None = None
    highlight: str | None = None
    shading: str | None = None
    vert_align: str | None = None        # super/subscript is semantic, not style
    char_spacing: Length | None = None    # w:spacing -- signed TWIPS
    # w:position is ST_SignedHpsMeasure: HALF-POINTS. Reading it as twips
    # makes a 3pt superscript raise by 0.15pt, which looks like no offset
    # at all rather than like a bug.
    position: HalfPoints | None = None
    lang: str | None = None
    # toggles
    bold: bool | None = None
    bold_cs: bool | None = None
    italic: bool | None = None
    italic_cs: bool | None = None
    caps: bool | None = None
    small_caps: bool | None = None
    strike: bool | None = None
    vanish: bool | None = None
    emboss: bool | None = None
    imprint: bool | None = None
    outline: bool | None = None
    shadow: bool | None = None

    @classmethod
    def parse(cls, rpr: etree._Element | None) -> RunProps:
        if rpr is None:
            return cls()
        rfonts = rpr.find(qn("w:rFonts"))
        color = rpr.find(qn("w:color"))
        shd = rpr.find(qn("w:shd"))
        spacing = rpr.find(qn("w:spacing"))
        position = rpr.find(qn("w:position"))
        return cls(
            style_id=_val(rpr, "w:rStyle"),
            font_ascii=rfonts.get(qn("w:ascii")) if rfonts is not None else None,
            font_hansi=rfonts.get(qn("w:hAnsi")) if rfonts is not None else None,
            font_cs=rfonts.get(qn("w:cs")) if rfonts is not None else None,
            font_ascii_theme=rfonts.get(qn("w:asciiTheme")) if rfonts is not None else None,
            font_hansi_theme=rfonts.get(qn("w:hAnsiTheme")) if rfonts is not None else None,
            size=FontSize.parse(_val(rpr, "w:sz")),
            size_cs=FontSize.parse(_val(rpr, "w:szCs")),
            color=color.get(qn("w:val")) if color is not None else None,
            color_theme=color.get(qn("w:themeColor")) if color is not None else None,
            underline=_val(rpr, "w:u"),
            highlight=_val(rpr, "w:highlight"),
            shading=shd.get(qn("w:fill")) if shd is not None else None,
            vert_align=_val(rpr, "w:vertAlign"),
            char_spacing=Length.parse(spacing.get(qn("w:val"))) if spacing is not None else None,
            position=HalfPoints.parse(position.get(qn("w:val"))) if position is not None else None,
            lang=_val(rpr, "w:lang"),
            bold=_on_off(rpr, "w:b"),
            bold_cs=_on_off(rpr, "w:bCs"),
            italic=_on_off(rpr, "w:i"),
            italic_cs=_on_off(rpr, "w:iCs"),
            caps=_on_off(rpr, "w:caps"),
            small_caps=_on_off(rpr, "w:smallCaps"),
            strike=_on_off(rpr, "w:strike"),
            vanish=_on_off(rpr, "w:vanish"),
            emboss=_on_off(rpr, "w:emboss"),
            imprint=_on_off(rpr, "w:imprint"),
            outline=_on_off(rpr, "w:outline"),
            shadow=_on_off(rpr, "w:shadow"),
        )

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))

    def specified(self) -> set[str]:
        return {f.name for f in fields(self) if getattr(self, f.name) is not None}


@dataclass(frozen=True)
class ParaProps:
    """A w:pPr at one level of the cascade."""

    style_id: str | None = None
    alignment: str | None = None
    space_before: Length | None = None
    space_after: Length | None = None
    space_before_auto: bool | None = None
    space_after_auto: bool | None = None
    line_spacing: LineSpacing | None = None
    indent_left: Length | None = None
    indent_right: Length | None = None
    indent_first_line: Length | None = None
    indent_hanging: Length | None = None
    outline_level: int | None = None
    num_id: int | None = None
    num_level: int | None = None
    contextual_spacing: bool | None = None
    keep_next: bool | None = None
    keep_lines: bool | None = None
    widow_control: bool | None = None
    page_break_before: bool | None = None
    shading: str | None = None
    has_borders: bool | None = None
    has_tabs: bool | None = None
    mark_run: RunProps | None = None   # w:pPr/w:rPr -- the paragraph mark

    @classmethod
    def parse(cls, ppr: etree._Element | None) -> ParaProps:
        if ppr is None:
            return cls()
        spacing = ppr.find(qn("w:spacing"))
        ind = ppr.find(qn("w:ind"))
        numpr = ppr.find(qn("w:numPr"))
        shd = ppr.find(qn("w:shd"))
        outline = _val(ppr, "w:outlineLvl")
        num_id = num_level = None
        if numpr is not None:
            num_id = _int(_val(numpr, "w:numId"))
            num_level = _int(_val(numpr, "w:ilvl"))
        # w:ind uses left/right in transitional files and start/end in strict.
        def ind_attr(*names: str) -> Length | None:
            if ind is None:
                return None
            for name in names:
                if (raw := ind.get(qn(name))) is not None:
                    return Length.parse(raw)
            return None

        return cls(
            style_id=_val(ppr, "w:pStyle"),
            alignment=_val(ppr, "w:jc"),
            space_before=Length.parse(spacing.get(qn("w:before"))) if spacing is not None else None,
            space_after=Length.parse(spacing.get(qn("w:after"))) if spacing is not None else None,
            space_before_auto=parse_on_off(
                spacing.get(qn("w:beforeAutospacing")), present=spacing is not None
            ) if spacing is not None and spacing.get(qn("w:beforeAutospacing")) is not None else None,
            space_after_auto=parse_on_off(
                spacing.get(qn("w:afterAutospacing")), present=spacing is not None
            ) if spacing is not None and spacing.get(qn("w:afterAutospacing")) is not None else None,
            line_spacing=LineSpacing.parse(
                spacing.get(qn("w:line")), spacing.get(qn("w:lineRule"))
            ) if spacing is not None else None,
            indent_left=ind_attr("w:left", "w:start"),
            indent_right=ind_attr("w:right", "w:end"),
            indent_first_line=ind_attr("w:firstLine"),
            indent_hanging=ind_attr("w:hanging"),
            outline_level=_int(outline),
            num_id=num_id,
            num_level=num_level,
            contextual_spacing=_on_off(ppr, "w:contextualSpacing"),
            keep_next=_on_off(ppr, "w:keepNext"),
            keep_lines=_on_off(ppr, "w:keepLines"),
            widow_control=_on_off(ppr, "w:widowControl"),
            page_break_before=_on_off(ppr, "w:pageBreakBefore"),
            shading=shd.get(qn("w:fill")) if shd is not None else None,
            has_borders=True if ppr.find(qn("w:pBdr")) is not None else None,
            has_tabs=True if ppr.find(qn("w:tabs")) is not None else None,
            mark_run=RunProps.parse(ppr.find(qn("w:rPr"))),
        )

    @property
    def is_empty(self) -> bool:
        return all(
            getattr(self, f.name) is None
            for f in fields(self)
            if f.name != "mark_run"
        )

    def specified(self) -> set[str]:
        return {
            f.name for f in fields(self)
            if f.name != "mark_run" and getattr(self, f.name) is not None
        }


def _int(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# w:rFonts attributes that must move together. Specifying EITHER half at a
# nearer level replaces BOTH.
#
# DISPUTED -- see docs/open-questions.md. ECMA 17.3.2.26 reads as though the
# two attributes inherit independently and asciiTheme always wins. We follow
# OpenXmlPowerTools' atomic-pair model instead, on the strength of a reductio:
# if a theme reference inherited from docDefaults always beat a nearer
# explicit w:ascii, then no run could ever override a themed default font --
# and picking a non-theme font in Word's font box demonstrably works. Settle
# it empirically with `formgen doctor` on a box that has Word.
ATOMIC_GROUPS: tuple[tuple[str, ...], ...] = (
    ("font_ascii", "font_ascii_theme"),
    ("font_hansi", "font_hansi_theme"),
)
_GROUPED = {name for group in ATOMIC_GROUPS for name in group}
_FIELD_CACHE: dict = {}


def _fields_of(kind) -> tuple[str, ...]:
    if kind not in _FIELD_CACHE:
        _FIELD_CACHE[kind] = tuple(f.name for f in fields(kind))
    return _FIELD_CACHE[kind]


def _nearest(levels: list, name: str):
    value = None
    for level in levels:
        got = getattr(level, name, None)
        if got is not None:
            value = got
    return value


def _nearest_groups(levels: list, out: dict) -> None:
    for group in ATOMIC_GROUPS:
        if not all(name in out or True for name in group):
            continue
        chosen = {name: None for name in group}
        for level in levels:
            if any(getattr(level, name, None) is not None for name in group):
                chosen = {name: getattr(level, name, None) for name in group}
        out.update(chosen)


def _fold_mark_run(levels: list):
    marks = [m for lv in levels if (m := getattr(lv, "mark_run", None)) is not None]
    return fold(marks, RunProps) if marks else None


def fold(levels: list, kind=RunProps):
    """Roll a basedOn chain (root first) into ONE cascade level.

    Plain inheritance throughout, toggles included: the nearest style that
    specifies a property wins. Toggles stay tri-state (bool | None) so the
    result is still a valid cascade level -- flattening them to bool here
    would make an empty style inject an explicit False and silently cancel
    whatever the document defaults said.
    """
    out: dict = {}
    has_groups = any(g[0] in _fields_of(kind) for g in ATOMIC_GROUPS)
    for name in _fields_of(kind):
        if name == "mark_run":
            out[name] = _fold_mark_run(levels)
        elif name not in _GROUPED:
            out[name] = _nearest(levels, name)
    if has_groups:
        _nearest_groups(levels, out)
    return replace(kind(), **out)


def resolve_toggle(values: list[bool | None]) -> bool:
    """XOR one toggle across style-hierarchy levels (direct formatting excluded).

    An absent level falls through; an explicit off resets; an on reverses the
    state below it.
    """
    state = False
    for value in values:
        if value is None:
            continue
        state = False if value is False else not state
    return state


def _finalize_toggle(doc_defaults, style_levels: list, direct, name: str) -> bool:
    """Resolve one toggle the way Word does, which is not the way ECMA says.

    ECMA-376 17.7.3 specifies val_table XOR val_paragraph XOR val_character.
    Word deviates: [MS-OI29500] 2.1.257 (table styles: 2.1.245b) states that
    "Word resets the value of the toggle property to the value specified by
    the paragraph style if a value is present; for example, a value of 1
    resets the property's state to True instead of toggling it." No such
    deviation is recorded for character styles, so the XOR survives there
    alone -- which is also the only term LibreOffice implements.

    So: table and paragraph styles RESET, character styles TOGGLE, direct
    formatting wins outright, and an unspecified level inherits the document
    default rather than False.
    """
    given = getattr(direct, name, None)
    if given is not None:
        return bool(given)

    default = getattr(doc_defaults, name, None)
    state = bool(default) if default is not None else False
    for level_kind, props in style_levels:
        value = getattr(props, name, None)
        if value is None:
            continue
        if value is False:
            # Per ECMA a false in a *style* is a no-op; Word resets. We follow
            # Word deliberately -- a reader checking against ECMA alone will
            # think this is a bug, hence this comment.
            state = False
        elif level_kind == "character":
            state = not state
        else:
            state = True
    return state


def finalize(doc_defaults, style_levels: list, direct, kind=RunProps):
    """Produce the effective properties Word would render.

    `style_levels` is an ordered list of ``(kind, props)`` pairs, where kind is
    "table", "paragraph" or "character" -- the kind matters, because toggles
    reset on the first two and toggle on the third.
    """
    out: dict = {}
    all_levels = [doc_defaults, *[props for _, props in style_levels], direct]
    has_groups = any(g[0] in _fields_of(kind) for g in ATOMIC_GROUPS)
    for name in _fields_of(kind):
        if name in TOGGLE_FIELDS:
            out[name] = _finalize_toggle(doc_defaults, style_levels, direct, name)
        elif name == "mark_run":
            out[name] = _fold_mark_run(all_levels)
        elif name not in _GROUPED:
            out[name] = _nearest(all_levels, name)
    if has_groups:
        _nearest_groups(all_levels, out)
    return replace(kind(), **out)


def effective_font(props: RunProps) -> str | None:
    """The typeface Word will actually use, honouring theme indirection.

    A theme reference beats an explicit typeface, which is why setting
    w:ascii without clearing w:asciiTheme is a silent no-op. Returns the theme
    token (e.g. 'minorHAnsi') for the caller to resolve against theme1.xml.
    """
    return props.font_ascii_theme or props.font_ascii
