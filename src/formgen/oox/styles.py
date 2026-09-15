"""The style graph and effective-property resolution.

This is the module everything else depends on, and it is where OOXML's most
expensive traps live:

* **w:docDefaults is the inheritance root, not Normal.** Some documents put the
  body font in docDefaults, others in Normal. Comparing unresolved values is
  the single largest source of false "these documents disagree" reports.
* **w:styleId is localised for built-ins** (Ueberschrift1) while w:name stays
  English (heading 1). Cross-document identity keys on the name.
* **A basedOn chain contributes ONE cascade level**, not one per style, which
  matters for toggle properties (ECMA-376 17.7.3).
* **A dangling basedOn falls back to docDefaults**, not to Normal.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import qn
from .props import ParaProps, RunProps, finalize, fold
from .theme import Theme


def normalize_style_name(name: str) -> str:
    """Canonical key for cross-document style identity.

    Lowercases and collapses whitespace. It deliberately does NOT strip a
    trailing ' Char': that suffix only means "linked character half" when
    w:link says so, and blindly stripping it merges a custom paragraph style
    named 'Sidebar' with an unrelated character style named 'Sidebar Char'.
    StyleGraph aliases genuine linked pairs using w:link instead.
    """
    return " ".join(name.split()).strip().lower()


def _attr_on(el: etree._Element, name: str) -> bool:
    """Read an ST_OnOff *attribute*. Legal values are 1 0 true false on off."""
    raw = (el.get(qn(name)) or "").strip().lower()
    return raw in ("1", "true", "on")


# Word ignores any child elements of a style with one of these ids, so
# formatting parsed from them would be formatting Word throws away --
# and DefaultParagraphFont is a very common basedOn target.
IGNORED_STYLE_CONTENT = frozenset({"NoList", "DefaultParagraphFont", "TableNormal"})


@dataclass
class Style:
    style_id: str
    name: str
    type: str = "paragraph"
    based_on: str | None = None
    next_style: str | None = None
    link: str | None = None
    custom: bool = False
    default: bool = False
    ui_priority: int | None = None
    q_format: bool = False
    semi_hidden: bool = False
    run: RunProps = field(default_factory=RunProps)
    para: ParaProps = field(default_factory=ParaProps)
    aliases: list[str] = field(default_factory=list)
    element: etree._Element | None = None

    @property
    def key(self) -> str:
        return normalize_style_name(self.name)

    @property
    def name_key(self) -> tuple[str, str]:
        """Identity key. Scoped by type: a paragraph and a character style may
        legitimately share a display name."""
        return (self.type, normalize_style_name(self.name))


@dataclass
class EffectiveStyle:
    """A style with its whole inheritance chain folded in."""

    style_id: str
    name: str
    type: str
    chain: list[str]
    run: RunProps
    para: ParaProps
    font: str | None = None       # theme-resolved typeface
    color: str | None = None      # theme-resolved hex
    resolved: bool = True         # False when the styleId is not defined here
    cyclic: bool = False          # basedOn chain contains a cycle
    color_is_themed: bool = False # colour came via w:themeColor


class StyleGraph:
    """Parsed styles.xml with inheritance resolution."""

    def __init__(
        self,
        styles: dict[str, Style],
        default_run: RunProps,
        default_para: ParaProps,
        theme: Theme | None = None,
        latent: dict[str, dict] | None = None,
    ):
        self.styles = styles
        self.default_run = default_run
        self.default_para = default_para
        self.theme = theme or Theme()
        self.latent = latent or {}
        self._by_name: dict[tuple[str, str], Style] = {}
        for style in styles.values():
            self._by_name.setdefault(style.name_key, style)
            for alias in style.aliases:
                self._by_name.setdefault(
                    (style.type, normalize_style_name(alias)), style
                )
        # Alias the character half of a genuine linked pair to its paragraph
        # style, but only where w:link actually says they are a pair.
        for style in styles.values():
            partner = styles.get(style.link) if style.link else None
            if partner is not None and partner.type != style.type:
                self._by_name.setdefault((partner.type, style.key), partner)
        self._cache: dict[str, EffectiveStyle] = {}
        self._chain_cache: dict[tuple[str, str], object] = {}

    # -- construction ---------------------------------------------------

    @classmethod
    def parse(cls, root: etree._Element, theme: Theme | None = None) -> StyleGraph:
        default_run = RunProps()
        default_para = ParaProps()
        docdefaults = root.find(qn("w:docDefaults"))
        if docdefaults is not None:
            rpr_default = docdefaults.find(f"{qn('w:rPrDefault')}/{qn('w:rPr')}")
            ppr_default = docdefaults.find(f"{qn('w:pPrDefault')}/{qn('w:pPr')}")
            default_run = RunProps.parse(rpr_default)
            default_para = ParaProps.parse(ppr_default)

        latent: dict[str, dict] = {}
        latent_el = root.find(qn("w:latentStyles"))
        if latent_el is not None:
            for exc in latent_el.findall(qn("w:lsdException")):
                if name := exc.get(qn("w:name")):
                    latent[normalize_style_name(name)] = dict(exc.attrib)

        styles: dict[str, Style] = {}
        for el in root.findall(qn("w:style")):
            style = cls._parse_style(el)
            if style:
                styles[style.style_id] = style
        return cls(styles, default_run, default_para, theme, latent)

    @staticmethod
    def _parse_style(el: etree._Element) -> Style | None:
        style_id = el.get(qn("w:styleId"))
        if not style_id:
            return None

        def val(tag: str) -> str | None:
            child = el.find(qn(tag))
            return child.get(qn("w:val")) if child is not None else None

        def flag(tag: str) -> bool:
            child = el.find(qn(tag))
            if child is None:
                return False
            return (child.get(qn("w:val")) or "1").lower() not in ("0", "false", "off")

        raw_name = val("w:name") or style_id
        # w:name may carry a comma-separated alias list; the first is canonical.
        names = [n.strip() for n in raw_name.split(",") if n.strip()]
        priority = val("w:uiPriority")
        return Style(
            style_id=style_id,
            name=names[0] if names else style_id,
            aliases=names[1:],
            type=el.get(qn("w:type")) or "paragraph",
            based_on=val("w:basedOn"),
            next_style=val("w:next"),
            link=val("w:link"),
            # ST_OnOff attributes: "off" and "false" are both off. Reading
            # w:default="off" as on picks the wrong default paragraph style.
            custom=_attr_on(el, "w:customStyle"),
            default=_attr_on(el, "w:default"),
            ui_priority=int(priority) if priority and priority.isdigit() else None,
            q_format=flag("w:qFormat"),
            semi_hidden=flag("w:semiHidden"),
            run=(RunProps() if style_id in IGNORED_STYLE_CONTENT
                 else RunProps.parse(el.find(qn("w:rPr")))),
            para=(ParaProps() if style_id in IGNORED_STYLE_CONTENT
                  else ParaProps.parse(el.find(qn("w:pPr")))),
            element=el,
        )

    # -- lookup ---------------------------------------------------------

    def __contains__(self, style_id: str) -> bool:
        return style_id in self.styles

    def __len__(self) -> int:
        return len(self.styles)

    def by_id(self, style_id: str | None) -> Style | None:
        return self.styles.get(style_id) if style_id else None

    def by_name(self, name: str | None, type: str = "paragraph") -> Style | None:
        """Look a style up by display name -- the cross-document identity key.

        Scoped by style type, so a character style cannot answer a query for a
        paragraph style of the same name.
        """
        if not name:
            return None
        return self._by_name.get((type, normalize_style_name(name)))

    def default_paragraph_style(self) -> Style | None:
        """The document's default paragraph style.

        The LAST style marked w:default in document order wins, per the spec
        and python-docx; taking the first picks the wrong one when a producer
        emits several.
        """
        found = [s for s in self.styles.values()
                 if s.type == "paragraph" and s.default]
        if found:
            return found[-1]
        return self.by_name("Normal")

    def ancestry(self, style_id: str, type: str | None = None) -> list[str]:
        """basedOn chain, root first, ending at `style_id`. Cycle-safe.

        Word tolerates some cyclic basedOn graphs, so we must too: a repeated
        id ends the walk rather than hanging. A basedOn that jumps style type
        (real in LibreOffice- and Aspose-produced files) is not followed.
        """
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = style_id
        while current and current in self.styles and current not in seen:
            style = self.styles[current]
            if type is not None and style.type != type:
                break
            seen.add(current)
            chain.append(current)
            current = style.based_on
        chain.reverse()
        return chain

    def has_cycle(self, style_id: str) -> bool:
        seen: set[str] = set()
        current: str | None = style_id
        while current and current in self.styles:
            if current in seen:
                return True
            seen.add(current)
            current = self.styles[current].based_on
        return False

    def dangling_based_on(self) -> list[tuple[str, str]]:
        """(styleId, missing basedOn target) -- these fall back to docDefaults."""
        return [
            (s.style_id, s.based_on)
            for s in self.styles.values()
            if s.based_on and s.based_on not in self.styles
        ]

    # -- resolution -----------------------------------------------------

    def _chain(self, style_id: str | None, kind: str, style_type: str = "paragraph"):
        """Rolled-up basedOn chain for one style, memoised."""
        key = (kind, style_id or "", style_type)
        if key not in self._chain_cache:
            ids = self.ancestry(style_id, style_type) if style_id else []
            attr = "run" if kind == "run" else "para"
            levels = [getattr(self.styles[sid], attr) for sid in ids]
            cls = RunProps if kind == "run" else ParaProps
            self._chain_cache[key] = fold(levels, cls) if levels else cls()
        return self._chain_cache[key]

    def effective(self, style_id: str | None) -> EffectiveStyle:
        """Fold document defaults and the whole basedOn chain into one result.

        The chain contributes a single cascade level (plain inheritance, not
        XOR), which is then finalised against document defaults per
        MS-OI29500 17.7.3.
        """
        key = style_id or ""
        if key in self._cache:
            return self._cache[key]

        style = self.by_id(style_id)
        stype = style.type if style else "paragraph"
        chain = self.ancestry(style_id, stype) if style_id else []
        chain_run = fold([self.styles[s].run for s in chain], RunProps) if chain else RunProps()
        chain_para = fold([self.styles[s].para for s in chain], ParaProps) if chain else ParaProps()

        level = "character" if stype == "character" else "paragraph"
        run = finalize(self.default_run, [(level, chain_run)], RunProps(), RunProps)
        para = finalize(self.default_para, [(level, chain_para)], ParaProps(), ParaProps)
        eff = EffectiveStyle(
            style_id=style_id or "",
            name=style.name if style else (style_id or ""),
            type=stype,
            chain=chain,
            run=run,
            para=para,
            font=self.theme.resolve_font(run.font_ascii, run.font_ascii_theme),
            color=self.theme.resolve_color(run.color, run.color_theme),
            color_is_themed=run.color_theme is not None,
            # A referenced-but-undefined styleId renders with Word's built-in
            # definition, which we do not have. Saying so beats returning
            # plausible-looking document defaults and letting a linter invent
            # a deviation.
            resolved=style is not None or not style_id,
            cyclic=bool(style_id) and self.has_cycle(style_id),
        )
        self._cache[key] = eff
        return eff

    def para_style_or_default(self, style_id: str | None) -> str | None:
        """Resolve a paragraph's style id, falling back to the default.

        Per 17.3.1.27, a w:pStyle that is omitted OR references a style which
        does not exist both mean "no paragraph style shall be applied", which
        then collapses to the default paragraph style. So a dangling reference
        needs no built-in style table -- it is just the absent case, and only
        the lint warning differs.
        """
        if style_id and style_id in self.styles:
            return style_id
        default = self.default_paragraph_style()
        return default.style_id if default else None

    def effective_for_run(
        self, style_id: str | None, char_style_id: str | None, direct: RunProps
    ) -> RunProps:
        """Full run cascade: docDefaults -> para style -> char style -> direct."""
        style_id = self.para_style_or_default(style_id)
        levels = []
        if style_id:
            levels.append(("paragraph", self._chain(style_id, "run", "paragraph")))
        if char_style_id:
            levels.append(("character", self._chain(char_style_id, "run", "character")))
        return finalize(self.default_run, levels, direct, RunProps)

    def effective_for_para(self, style_id: str | None, direct: ParaProps) -> ParaProps:
        style_id = self.para_style_or_default(style_id)
        levels = ([("paragraph", self._chain(style_id, "para", "paragraph"))]
                  if style_id else [])
        return finalize(self.default_para, levels, direct, ParaProps)

    def outline_level_of(self, style_id: str | None) -> int | None:
        """Outline level resolved through the chain -- a strong heading signal.

        Level 9 is not "heading 9": Word writes w:outlineLvl val="9" to mean
        body text, i.e. to remove an inherited outline level. It is reported
        as None so that `is not None` reads as "is a heading".
        """
        level = self.effective(style_id).para.outline_level
        return None if level is None or level >= 9 else level
