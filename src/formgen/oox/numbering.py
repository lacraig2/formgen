"""List definitions: numbering.xml, and what a paragraph's numbering resolves to.

Numbering is the part of OOXML most likely to be silently wrong, because the
identifiers are document-local and the indirection is three deep:

    w:numPr/w:numId -> w:num -> w:abstractNumId -> w:abstractNum -> w:lvl[@w:ilvl]

with two side doors on top of it. ``w:numStyleLink`` makes an abstract
definition an empty pointer at *a style*, whose own numbering holds the real
levels; ``w:lvlOverride`` re-points or restarts one level of one instance.

The traps encoded here:

* **numId is document-local.** Two documents that both say ``numId="3"`` are
  saying nothing to each other. Cross-document identity is
  :meth:`Numbering.signature` -- a canonical per-level shape with every local
  id removed.
* **numId="0" means "explicitly not numbered"**, which is not the same as an
  absent w:numPr: it *removes* numbering inherited from the paragraph style.
  Treating 0 as a lookup that happens to miss gives the right answer here by
  accident and the wrong one when reporting why.
* **A level can name its own paragraph style** (``w:lvl/w:pStyle``). That is
  how numbered headings work: the paragraph carries no ilvl at all and the
  level is found by matching the style. Ignoring it numbers every heading "1".
* **Bullet glyphs are font-dependent.** The same bullet is U+F0B7 in Symbol
  and U+2022 in Arial. Voting on the raw w:lvlText across a corpus therefore
  finds disagreement where there is none, so lvlText is canonicalised against
  the level's own font before it reaches a signature.
* **w:tentative levels are Word's invention**, not the author's: Word pads a
  hybridMultilevel definition out to nine levels and overwrites them the
  moment the user indents that far. They are parsed, flagged, and should carry
  no weight in consensus.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from lxml import etree

from ..opc.ns import qn
from .props import ParaProps, RunProps
from .values import Length

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .styles import StyleGraph


# Word's three default bullet levels, plus the handful of dingbats that turn up
# in house formats. The key is (font, codepoint); the value is the Unicode
# character an author would recognise. Anything not listed passes through
# unchanged -- a wrong guess here would merge two genuinely different bullets.
_BULLET_GLYPHS = {
    ("symbol", ""): "•",      # bullet
    ("symbol", ""): "▪",      # black small square
    ("symbol", ""): "➢",      # three-d right arrowhead
    ("symbol", ""): "✔",      # check mark
    ("symbol", ""): "❑",      # shadowed square
    ("symbol", ""): "–",      # en dash
    ("wingdings", ""): "▪",
    ("wingdings", ""): "◆",   # diamond
    ("wingdings", ""): "✔",
    ("wingdings", ""): "□",
    ("courier new", "o"): "o",
}

# w:numFmt values that produce no visible marker at all.
BULLET_FORMATS = frozenset({"bullet"})
NO_MARKER_FORMATS = frozenset({"none"})


def canonical_bullet(lvl_text: str | None, font: str | None) -> str | None:
    """Map a private-use bullet glyph to the character it represents.

    Word stores bullets as codepoints in the Symbol/Wingdings private use
    area, so the same bullet is a different string depending on the font the
    level happens to use. Left alone, that makes every corpus look as though
    its authors disagreed about bullets.
    """
    if not lvl_text:
        return lvl_text
    key = (font or "").strip().lower()
    return "".join(_BULLET_GLYPHS.get((key, ch), ch) for ch in lvl_text)


def _val(parent: etree._Element, tag: str) -> str | None:
    el = parent.find(qn(tag))
    return el.get(qn("w:val")) if el is not None else None


def _int_val(parent: etree._Element, tag: str) -> int | None:
    raw = _val(parent, tag)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _flag(parent: etree._Element, tag: str) -> bool:
    el = parent.find(qn(tag))
    if el is None:
        return False
    return (el.get(qn("w:val")) or "1").strip().lower() not in ("0", "false", "off")


def _attr_int(el: etree._Element, name: str) -> int | None:
    raw = el.get(qn(name))
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@dataclass(frozen=True)
class Level:
    """One w:lvl: what the marker looks like and where the text sits."""

    ilvl: int
    start: int | None = None
    num_fmt: str | None = None        # decimal | bullet | lowerLetter | none | ...
    lvl_text: str | None = None       # "%1." -- %N interpolates level N's counter
    lvl_jc: str | None = None
    suffix: str | None = None         # w:suff: tab (default) | space | nothing
    is_legal: bool = False            # render all inherited levels as decimal
    restart: int | None = None        # w:lvlRestart; 0 means "never restart"
    para_style: str | None = None     # w:pStyle -- the style this level numbers
    pic_bullet_id: int | None = None
    tentative: bool = False           # Word's padding, not the author's choice
    para: ParaProps = field(default_factory=ParaProps)
    run: RunProps = field(default_factory=RunProps)

    @classmethod
    def parse(cls, el: etree._Element) -> Level:
        return cls(
            ilvl=_attr_int(el, "w:ilvl") or 0,
            start=_int_val(el, "w:start"),
            num_fmt=_val(el, "w:numFmt"),
            lvl_text=_val(el, "w:lvlText"),
            lvl_jc=_val(el, "w:lvlJc"),
            suffix=_val(el, "w:suff"),
            is_legal=_flag(el, "w:isLgl"),
            restart=_int_val(el, "w:lvlRestart"),
            para_style=_val(el, "w:pStyle"),
            pic_bullet_id=_int_val(el, "w:lvlPicBulletId"),
            # ST_OnOff attribute, so "off"/"false"/"0" are all off.
            tentative=(el.get(qn("w:tentative")) or "0").strip().lower()
            in ("1", "true", "on"),
            para=ParaProps.parse(el.find(qn("w:pPr"))),
            run=RunProps.parse(el.find(qn("w:rPr"))),
        )

    @property
    def is_bullet(self) -> bool:
        return (self.num_fmt or "") in BULLET_FORMATS

    @property
    def is_unmarked(self) -> bool:
        """numFmt="none" -- a level that indents but shows no marker."""
        return (self.num_fmt or "") in NO_MARKER_FORMATS

    @property
    def bullet_font(self) -> str | None:
        return self.run.font_ascii

    @property
    def marker(self) -> str | None:
        """The level text with font-specific bullet codepoints canonicalised."""
        if self.is_bullet:
            return canonical_bullet(self.lvl_text, self.bullet_font)
        return self.lvl_text

    @property
    def indent_left(self) -> Length | None:
        return self.para.indent_left

    @property
    def indent_hanging(self) -> Length | None:
        return self.para.indent_hanging

    def signature(self) -> tuple:
        """Canonical shape of this level, free of document-local identifiers.

        Indents are included because they are part of what a house format
        *is*; `start` is included because a list that begins at 1 and one that
        begins at 5 are different definitions to an author looking at them.
        """
        return (
            self.ilvl,
            self.num_fmt or "decimal",
            self.marker or "",
            self.lvl_jc or "left",
            self.suffix or "tab",
            self.is_legal,
            self.start if self.start is not None else 1,
            self.indent_left.twips if self.indent_left else None,
            self.indent_hanging.twips if self.indent_hanging else None,
            (self.bullet_font or "").lower() if self.is_bullet else None,
        )

    def shape(self) -> tuple:
        """Looser key: marker format and text only, ignoring geometry.

        Two documents whose bullet lists are indented differently still use
        the same *kind* of list, and for some questions that is the join we
        want. Kept separate from `signature` so callers choose deliberately.
        """
        return (self.ilvl, self.num_fmt or "decimal", self.marker or "")


@dataclass(frozen=True)
class LevelOverride:
    ilvl: int
    start_override: int | None = None
    level: Level | None = None       # a full replacement, not a merge


@dataclass
class AbstractNum:
    """A w:abstractNum -- the reusable definition N instances point at."""

    abstract_id: int
    levels: dict[int, Level] = field(default_factory=dict)
    nsid: str | None = None          # must be unique; duplicates make Word merge lists
    tmpl: str | None = None
    multi_level_type: str | None = None
    name: str | None = None
    style_link: str | None = None    # this definition IS that style's numbering
    num_style_link: str | None = None  # this definition DEFERS to that style's

    @classmethod
    def parse(cls, el: etree._Element) -> AbstractNum | None:
        aid = _attr_int(el, "w:abstractNumId")
        if aid is None:
            return None
        levels = {}
        for lvl_el in el.findall(qn("w:lvl")):
            lvl = Level.parse(lvl_el)
            levels[lvl.ilvl] = lvl
        return cls(
            abstract_id=aid,
            levels=levels,
            nsid=_val(el, "w:nsid"),
            tmpl=_val(el, "w:tmpl"),
            multi_level_type=_val(el, "w:multiLevelType"),
            name=_val(el, "w:name"),
            style_link=_val(el, "w:styleLink"),
            num_style_link=_val(el, "w:numStyleLink"),
        )


@dataclass
class NumInstance:
    """A w:num -- one *use* of an abstract definition, with its overrides."""

    num_id: int
    abstract_id: int | None = None
    overrides: dict[int, LevelOverride] = field(default_factory=dict)

    @classmethod
    def parse(cls, el: etree._Element) -> NumInstance | None:
        nid = _attr_int(el, "w:numId")
        if nid is None:
            return None
        overrides: dict[int, LevelOverride] = {}
        for ov in el.findall(qn("w:lvlOverride")):
            ilvl = _attr_int(ov, "w:ilvl")
            if ilvl is None:
                continue
            lvl_el = ov.find(qn("w:lvl"))
            overrides[ilvl] = LevelOverride(
                ilvl=ilvl,
                start_override=_int_val(ov, "w:startOverride"),
                level=Level.parse(lvl_el) if lvl_el is not None else None,
            )
        return cls(
            num_id=nid,
            abstract_id=_int_val(el, "w:abstractNumId"),
            overrides=overrides,
        )


@dataclass(frozen=True)
class NumRef:
    """What a paragraph's numbering actually resolved to."""

    num_id: int
    ilvl: int
    level: Level | None
    source: str        # "direct" | "style" | "level-pstyle"
    resolved: bool = True

    @property
    def is_bullet(self) -> bool:
        return bool(self.level and self.level.is_bullet)


class Numbering:
    """Parsed numbering.xml with the indirection resolved."""

    def __init__(
        self,
        abstracts: dict[int, AbstractNum] | None = None,
        instances: dict[int, NumInstance] | None = None,
        styles: StyleGraph | None = None,
    ):
        self.abstracts = abstracts or {}
        self.instances = instances or {}
        self.styles = styles
        self._level_cache: dict[tuple[int, int], Level | None] = {}
        self._style_levels: dict[str, tuple[int, int]] | None = None

    @classmethod
    def parse(
        cls, root: etree._Element | None, styles: StyleGraph | None = None
    ) -> Numbering:
        if root is None:
            return cls(styles=styles)
        abstracts: dict[int, AbstractNum] = {}
        for el in root.findall(qn("w:abstractNum")):
            if (a := AbstractNum.parse(el)) is not None:
                abstracts[a.abstract_id] = a
        instances: dict[int, NumInstance] = {}
        for el in root.findall(qn("w:num")):
            if (n := NumInstance.parse(el)) is not None:
                instances[n.num_id] = n
        return cls(abstracts, instances, styles)

    def bind(self, styles: StyleGraph) -> Numbering:
        """Attach a style graph so w:numStyleLink can be followed."""
        self.styles = styles
        self._level_cache.clear()
        self._style_levels = None
        return self

    def __len__(self) -> int:
        return len(self.instances)

    def __contains__(self, num_id: int) -> bool:
        return num_id in self.instances

    # -- indirection ----------------------------------------------------

    def abstract_for(self, num_id: int) -> AbstractNum | None:
        """Follow numId -> num -> abstractNum, chasing w:numStyleLink.

        A numStyleLink abstract carries no levels of its own: it names a
        paragraph style, whose numbering names the abstract that does. The
        walk is cycle-guarded because Word tolerates definitions that point at
        each other and we must not hang on one.
        """
        seen: set[int] = set()
        instance = self.instances.get(num_id)
        while instance is not None:
            aid = instance.abstract_id
            if aid is None or aid not in self.abstracts or aid in seen:
                return None
            seen.add(aid)
            abstract = self.abstracts[aid]
            if not abstract.num_style_link:
                return abstract
            nxt = self._num_id_for_style(abstract.num_style_link)
            if nxt is None or nxt == num_id:
                # The link is dangling, or points back at us. The abstract's
                # own levels are the best answer left -- usually empty, which
                # the caller sees as an unresolved NumRef.
                return abstract
            num_id, instance = nxt, self.instances.get(nxt)
        return None

    def _num_id_for_style(self, style_id: str) -> int | None:
        """The numId a paragraph style carries, through its basedOn chain."""
        if self.styles is None:
            return None
        # Prefer the styleLink that names this style outright: that is the
        # abstract definition declaring itself to BE the style's numbering,
        # and it is authoritative even when the style's own pPr is silent.
        for abstract in self.abstracts.values():
            if abstract.style_link == style_id:
                for inst in self.instances.values():
                    if inst.abstract_id == abstract.abstract_id:
                        return inst.num_id
        props = self.styles.effective(style_id).para
        return props.num_id

    def level(self, num_id: int, ilvl: int) -> Level | None:
        """The fully resolved level, with any w:lvlOverride applied.

        A lvlOverride carrying a w:lvl REPLACES the abstract level outright --
        it is not merged. A startOverride adjusts only the start value.
        """
        key = (num_id, ilvl)
        if key in self._level_cache:
            return self._level_cache[key]
        result = self._level_uncached(num_id, ilvl)
        self._level_cache[key] = result
        return result

    def _level_uncached(self, num_id: int, ilvl: int) -> Level | None:
        instance = self.instances.get(num_id)
        override = instance.overrides.get(ilvl) if instance else None
        if override is not None and override.level is not None:
            base = override.level
        else:
            abstract = self.abstract_for(num_id)
            if abstract is None:
                return None
            base = abstract.levels.get(ilvl)
            if base is None:
                return None
        if override is not None and override.start_override is not None:
            base = replace(base, start=override.start_override)
        return base

    def levels(self, num_id: int) -> dict[int, Level]:
        """Every defined level of one instance, overrides applied."""
        abstract = self.abstract_for(num_id)
        instance = self.instances.get(num_id)
        ilvls = set(abstract.levels) if abstract else set()
        if instance:
            ilvls |= set(instance.overrides)
        return {i: lvl for i in sorted(ilvls) if (lvl := self.level(num_id, i))}

    # -- cross-document identity ----------------------------------------

    def signature(self, num_id: int, include_tentative: bool = False) -> tuple | None:
        """Canonical shape of a whole list definition.

        This is the only legitimate cross-document join key for lists, since
        numId, abstractNumId, nsid and tmpl are all local to one file.
        Tentative levels are excluded by default: they are Word's padding, and
        including them makes two identical three-level lists look different
        because Word guessed differently about levels 4-9.
        """
        levels = self.levels(num_id)
        if not levels:
            return None
        return tuple(
            lvl.signature()
            for _, lvl in sorted(levels.items())
            if include_tentative or not lvl.tentative
        ) or None

    def shape(self, num_id: int) -> tuple | None:
        """Looser join key: marker formats only, geometry ignored."""
        levels = self.levels(num_id)
        if not levels:
            return None
        return tuple(
            lvl.shape() for _, lvl in sorted(levels.items()) if not lvl.tentative
        ) or None

    # -- diagnostics ----------------------------------------------------

    def duplicate_nsids(self) -> dict[str, list[int]]:
        """nsid -> abstract ids sharing it. Word merges lists that collide."""
        seen: dict[str, list[int]] = {}
        for abstract in self.abstracts.values():
            if abstract.nsid:
                seen.setdefault(abstract.nsid.lower(), []).append(abstract.abstract_id)
        return {k: v for k, v in seen.items() if len(v) > 1}

    def dangling_instances(self) -> list[int]:
        """numIds whose abstract definition is missing -- Word renders nothing."""
        return sorted(
            n for n in self.instances if self.abstract_for(n) is None
        )

    def style_levels(self) -> dict[str, tuple[int, int]]:
        """styleId -> (numId, ilvl) for every level that names a style.

        This is the numbered-heading mechanism: the paragraph carries no
        numPr, and the level finds the paragraph rather than the other way
        round.

        Memoised because `resolve` consults it for EVERY paragraph: rebuilding
        it each time turns a document walk into an O(paragraphs x lists) scan.
        """
        if self._style_levels is not None:
            return self._style_levels
        out: dict[str, tuple[int, int]] = {}
        for num_id in sorted(self.instances):
            for ilvl, lvl in self.levels(num_id).items():
                if lvl.para_style:
                    out.setdefault(lvl.para_style, (num_id, ilvl))
        self._style_levels = out
        return out

    # -- paragraph resolution -------------------------------------------

    def resolve(
        self, num_id: int | None, ilvl: int | None, style_id: str | None = None
    ) -> NumRef | None:
        """Resolve one paragraph's effective numbering.

        `num_id` and `ilvl` are the values already cascaded through the style
        chain (see :meth:`StyleGraph.effective_for_para`), not the raw w:numPr.
        """
        if num_id is None:
            # No numPr anywhere in the cascade -- but a level may still claim
            # this style by name.
            if style_id:
                found = self.style_levels().get(style_id)
                if found:
                    n, lv = found
                    return NumRef(n, lv, self.level(n, lv), "level-pstyle")
            return None
        if num_id == 0:
            # Explicitly not numbered. Distinct from absent: this REMOVES
            # numbering the paragraph style would otherwise have applied.
            return None
        if ilvl is None:
            # A style-linked level pins the level even when the paragraph
            # gives none; otherwise ilvl defaults to 0 per 17.9.3.
            if style_id:
                found = self.style_levels().get(style_id)
                if found and found[0] == num_id:
                    return NumRef(num_id, found[1],
                                  self.level(num_id, found[1]), "level-pstyle")
            ilvl = 0
        level = self.level(num_id, ilvl)
        return NumRef(num_id, ilvl, level, "style", resolved=level is not None)


def numbering_of(
    styles: StyleGraph,
    numbering: Numbering | None,
    style_id: str | None,
    direct: ParaProps,
) -> NumRef | None:
    """Effective numbering for a paragraph, style cascade included.

    Note that num_id and num_level cascade *independently*: a style supplying
    numId=3/ilvl=0 plus a direct ilvl=2 yields (3, 2), which is what Word
    shows. Whether a direct numId alone should also reset the level to 0 --
    i.e. whether w:numPr merges element-wise or replaces as a unit -- is
    recorded as an open question, since the two readings differ only for a
    paragraph that specifies one half and inherits the other.
    """
    if numbering is None:
        return None
    props = styles.effective_for_para(style_id, direct)
    resolved_style = styles.para_style_or_default(style_id)
    ref = numbering.resolve(props.num_id, props.num_level, resolved_style)
    if ref is not None and direct.num_id is not None:
        return NumRef(ref.num_id, ref.ilvl, ref.level, "direct", ref.resolved)
    return ref
