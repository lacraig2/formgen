"""One document -> the observations a corpus votes on.

`learn` is a vote, and this is the ballot. Everything here answers the same
question in different places: *what did this document actually decide?* --
where "actually" is the resolved value Word renders, never the raw attribute.

Three rules shape the output:

**Observe resolved values, never raw ones.** Two documents that put the body
font in `docDefaults` and in `Normal` respectively have made the same
decision, and comparing their unresolved XML says they disagree. This is the
#1 source of false conflict reports, so every style observation runs through
the resolver first.

**Key on things that survive crossing a file boundary.** Style names, not
styleIds (localised for built-ins). List signatures, not numIds (document
local). The section that holds the body, not "section 1" (a cover page is a
section in some exemplars and not in others).

**Emit per-paragraph observations as well as per-style ones.** A style says
what the author declared; the paragraphs say what they got. The gap between
the two is direct formatting, and measuring it is what lets donor selection
reject a document whose authors fought the styles -- exactly the document that
looks most correct and makes the worst template.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from ..analyze.stats import bucket
from ..oox.numbering import Numbering
from ..oox.props import ParaProps, RunProps
from ..oox.sections import SectionModel
from ..oox.settings import Settings
from ..oox.styles import StyleGraph, normalize_style_name
from ..oox.theme import Theme
from ..oox.walk import Walker, match_key
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from ..util.pointer import ptr

# Properties a house format actually specifies. Observing every field of
# RunProps would bury the signal in lang tags and complex-script duplicates.
RUN_PROPS = (
    "font_ascii", "font_ascii_theme", "size", "color", "color_theme",
    "bold", "italic", "underline", "caps", "small_caps", "vert_align",
)
PARA_PROPS = (
    "alignment", "space_before", "space_after", "line_spacing",
    "indent_left", "indent_right", "indent_first_line", "indent_hanging",
    "outline_level", "keep_next", "keep_lines", "contextual_spacing",
    "page_break_before", "widow_control",
)
# What a paragraph is observed to have RENDERED as, which is a smaller set:
# these are the properties whose drift is visible and worth linting.
RENDERED_PROPS = ("size", "font_ascii", "bold", "italic")
RENDERED_PARA_PROPS = (
    "alignment", "space_after", "line_spacing", "indent_left", "indent_first_line",
)

# rPr children that are presentational rather than semantic. A run carrying
# any of these is a run whose author reached past the styles.
_DIRECT_MARKERS = (
    "w:rFonts", "w:sz", "w:szCs", "w:color", "w:highlight", "w:shd",
    "w:spacing", "w:position", "w:kern", "w:u", "w:bdr", "w:em",
)


@dataclass
class DocMeta:
    """Everything donor selection scores on, and everything lint refuses over."""

    doc: str
    sha256: str | None = None
    paragraphs: int = 0
    blocks: int = 0
    runs: int = 0
    direct_runs: int = 0
    styles_used: tuple[str, ...] = ()
    styles_defined: int = 0
    section_count: int = 1
    list_count: int = 0
    header_parts: int = 0
    footer_parts: int = 0
    has_textbox: bool = False
    has_alt_chunk: bool = False
    has_tracked_changes: bool = False
    has_comments: bool = False
    has_content_controls: bool = False
    disqualified: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def direct_density(self) -> float:
        """Share of runs carrying presentational direct formatting.

        The donor-selection penalty. A document at 0.6 looks right and is a
        terrible template: its format lives in its runs, not its styles, so
        nothing useful survives being cloned.
        """
        return self.direct_runs / self.runs if self.runs else 0.0

    @property
    def usable_as_donor(self) -> bool:
        return not self.disqualified


@dataclass
class DocObservations:
    """One document's ballot: pointer -> every value it offered."""

    doc: str
    values: dict[str, list[Any]] = field(default_factory=dict)
    meta: DocMeta = None  # type: ignore[assignment]

    def add(self, pointer: str, value: Any) -> None:
        if value is None:
            return
        self.values.setdefault(pointer, []).append(value)

    def single(self) -> dict[str, Any]:
        """Collapse to one value per pointer -- the modal one.

        This is the form profile_distance() compares, so a document with one
        stray italic paragraph is not counted as disagreeing with itself.
        """
        out: dict[str, Any] = {}
        for pointer, values in self.values.items():
            if len(values) == 1:
                out[pointer] = values[0]
                continue
            counts = Counter(bucket(v, pointer.rsplit("/", 1)[-1]) for v in values)
            modal = min(counts.items(), key=lambda kv: (-kv[1], repr(kv[0])))[0]
            out[pointer] = next(
                v for v in values if bucket(v, pointer.rsplit("/", 1)[-1]) == modal
            )
        return out

    def ballots(self) -> list[tuple[str, str, Any]]:
        """(pointer, doc, value) triples, ready for stats.vote_all."""
        return [
            (pointer, self.doc, value)
            for pointer, values in self.values.items()
            for value in values
        ]


def _part_root(pkg: OpcPackage, reltype: str) -> etree._Element | None:
    name = pkg.related(RT[reltype])
    return pkg.element(name) if name and name in pkg else None


def observe(pkg: OpcPackage, doc: str) -> DocObservations:
    """Read one exemplar into a ballot."""
    obs = DocObservations(doc=doc)
    meta = DocMeta(doc=doc)

    theme = Theme.parse(_part_root(pkg, "theme"))
    styles_root = _part_root(pkg, "styles")
    styles = StyleGraph.parse(styles_root, theme) if styles_root is not None else \
        StyleGraph({}, RunProps(), ParaProps(), theme)
    numbering = Numbering.parse(_part_root(pkg, "numbering"), styles)
    settings = Settings.parse(_part_root(pkg, "settings"))
    sections = SectionModel.from_package(pkg, settings)

    meta.styles_defined = len(styles)
    meta.section_count = len(sections)
    meta.list_count = len(numbering)
    meta.header_parts = len(dict.fromkeys(pkg.related_all(RT["header"])))
    meta.footer_parts = len(dict.fromkeys(pkg.related_all(RT["footer"])))
    meta.has_comments = pkg.related(RT["comments"]) is not None

    _observe_defaults(obs, styles)
    _observe_theme(obs, theme)
    _observe_settings(obs, settings)
    used = _observe_blocks(obs, meta, pkg, styles, sections)
    _observe_styles(obs, styles, used)
    _observe_sections(obs, sections, _primary_section(pkg, sections))
    _observe_lists(obs, numbering, used_lists=_used_lists(pkg, styles, numbering))
    _observe_hdrftr(obs, pkg, sections)

    meta.styles_used = tuple(sorted(used))
    meta.has_tracked_changes = settings.track_changes or _has_revisions(pkg)
    meta.disqualified = tuple(
        settings.refusal_reasons
        + (["imports content via w:altChunk, which cannot be inspected"]
           if meta.has_alt_chunk else [])
    )
    obs.meta = meta
    return obs


# -- document defaults and theme -----------------------------------------


def _observe_defaults(obs: DocObservations, styles: StyleGraph) -> None:
    """docDefaults is the inheritance root, so it is observed in its own right.

    It is also observed *through* every style, which is the point: a document
    that sets the body font here and one that sets it on Normal must produce
    the same style observations, and they do because the resolver ran first.
    """
    for name in RUN_PROPS:
        obs.add(ptr("defaults", "run", name), getattr(styles.default_run, name, None))
    for name in PARA_PROPS:
        obs.add(ptr("defaults", "para", name), getattr(styles.default_para, name, None))


def _observe_theme(obs: DocObservations, theme: Theme) -> None:
    obs.add(ptr("theme", "major_font"), theme.font("majorHAnsi"))
    obs.add(ptr("theme", "minor_font"), theme.font("minorHAnsi"))
    for slot, value in sorted(theme.colors.items()):
        obs.add(ptr("theme", "colors", slot), value)


def _observe_settings(obs: DocObservations, settings: Settings) -> None:
    obs.add(ptr("settings", "even_and_odd_headers"), settings.even_and_odd_headers)
    obs.add(ptr("settings", "mirror_margins"), settings.mirror_margins)
    obs.add(ptr("settings", "default_tab_stop"), settings.default_tab_stop)
    obs.add(ptr("settings", "compat_mode"), settings.compat_mode)


# -- styles ---------------------------------------------------------------


def _observe_styles(
    obs: DocObservations, styles: StyleGraph, used: set[str]
) -> None:
    """One observation per used style per property, fully resolved.

    Only styles the document USES vote. A file carrying Word's 300 latent
    style definitions has not thereby expressed an opinion about any of them,
    and letting it vote would drown out the handful its author chose.
    """
    for style in styles.styles.values():
        key = normalize_style_name(style.name)
        if key not in used:
            continue
        eff = styles.effective(style.style_id)
        base = ptr("styles", style.type, key)
        for name in RUN_PROPS:
            obs.add(f"{base}/run/{name}", getattr(eff.run, name, None))
        for name in PARA_PROPS:
            obs.add(f"{base}/para/{name}", getattr(eff.para, name, None))
        obs.add(f"{base}/font", eff.font)
        obs.add(f"{base}/color_resolved", eff.color)
        obs.add(f"{base}/based_on", _based_on_name(styles, style.based_on))
        obs.add(f"{base}/next", _based_on_name(styles, style.next_style))
        obs.add(f"{base}/q_format", style.q_format)


def _based_on_name(styles: StyleGraph, style_id: str | None) -> str | None:
    """Style references are stored as ids; ids do not cross documents."""
    target = styles.by_id(style_id)
    return normalize_style_name(target.name) if target else None


# -- blocks: what the paragraphs actually rendered as ---------------------


def _observe_blocks(
    obs: DocObservations,
    meta: DocMeta,
    pkg: OpcPackage,
    styles: StyleGraph,
    sections: SectionModel,
) -> set[str]:
    used: set[str] = set()
    for block in Walker(pkg).blocks():
        meta.blocks += 1
        if block.context.in_textbox:
            meta.has_textbox = True
        if block.context.in_sdt:
            meta.has_content_controls = True
        if block.kind == "altChunk":
            meta.has_alt_chunk = True
        if not block.is_paragraph or block.context.in_del:
            continue
        meta.paragraphs += 1

        style_id = styles.para_style_or_default(block.style_id)
        style = styles.by_id(style_id)
        name = normalize_style_name(style.name) if style else (style_id or "normal")
        used.add(name)

        direct_para = ParaProps.parse(block.element.find(qn("w:pPr")))
        eff_para = styles.effective_for_para(style_id, direct_para)
        base = ptr("rendered", name)
        for prop in RENDERED_PARA_PROPS:
            obs.add(f"{base}/para/{prop}", getattr(eff_para, prop, None))

        first = True
        for run in _runs(block.element):
            meta.runs += 1
            rpr = run.find(qn("w:rPr"))
            if rpr is not None and any(
                rpr.find(qn(tag)) is not None for tag in _DIRECT_MARKERS
            ):
                meta.direct_runs += 1
            if first:
                direct_run = RunProps.parse(rpr)
                eff_run = styles.effective_for_run(
                    style_id, direct_run.style_id, direct_run
                )
                for prop in RENDERED_PROPS:
                    obs.add(f"{base}/run/{prop}", getattr(eff_run, prop, None))
                first = False
    return used


def _runs(paragraph: etree._Element):
    """Direct runs of a paragraph, skipping the paragraph-mark rPr.

    w:pPr/w:rPr describes the pilcrow, not any text, so counting it as a run
    would report direct formatting in every paragraph Word has ever saved.
    """
    for child in paragraph.iter(qn("w:r")):
        parent = child.getparent()
        if parent is not None and parent.tag == qn("w:pPr"):
            continue
        yield child


def _has_revisions(pkg: OpcPackage) -> bool:
    """Tracked changes present in the body, whatever settings.xml claims.

    w:trackChanges only says recording is ON now; revisions can be sitting in
    a document with the switch off, and restyling those is the thing we must
    refuse.
    """
    root = pkg.element(pkg.main_document)
    for tag in ("w:ins", "w:del", "w:moveFrom", "w:moveTo", "w:pPrChange",
                "w:rPrChange", "w:sectPrChange", "w:tblPrChange"):
        if root.find(f".//{qn(tag)}") is not None:
            return True
    return False


# -- page setup -----------------------------------------------------------


def _primary_section(pkg: OpcPackage, sections: SectionModel) -> int:
    """The section holding the bulk of the body text.

    Section *index* is a poor cross-document key: a cover page is its own
    section in some exemplars and not in others, so "/page/sections/0" means
    the cover in one file and the body in another. The primary section is the
    one an author would call "the page setup", and it joins reliably.
    """
    if len(sections) == 1:
        return 0
    counts: Counter[int] = Counter()
    for block in Walker(pkg).blocks(include_aux=False):
        if block.is_paragraph and block.text.strip():
            counts[sections.section_of(block.element)] += 1
    return counts.most_common(1)[0][0] if counts else 0


def _observe_sections(
    obs: DocObservations, sections: SectionModel, primary: int
) -> None:
    obs.add(ptr("page", "section_count"), len(sections))
    section = sections[primary]
    base = ptr("page", "primary")
    obs.add(f"{base}/orientation", section.page.orientation)
    obs.add(f"{base}/width", section.page.width)
    obs.add(f"{base}/height", section.page.height)
    for name in ("top", "right", "bottom", "left", "header", "footer", "gutter"):
        obs.add(f"{base}/margins/{name}", getattr(section.margins, name))
    obs.add(f"{base}/columns", section.columns.count)
    obs.add(f"{base}/title_page", section.title_page)
    obs.add(f"{base}/page_number_format", section.page_numbering.fmt)
    obs.add(f"{base}/content_width", section.content_width)
    obs.add(f"{base}/vertical_align", section.vertical_align)


# -- lists ----------------------------------------------------------------


def _used_lists(
    pkg: OpcPackage, styles: StyleGraph, numbering: Numbering
) -> Counter:
    """numId -> paragraphs using it, so an unused definition cannot vote."""
    counts: Counter[int] = Counter()
    for block in Walker(pkg).blocks():
        if not block.is_paragraph:
            continue
        direct = ParaProps.parse(block.element.find(qn("w:pPr")))
        props = styles.effective_for_para(
            styles.para_style_or_default(block.style_id), direct
        )
        if props.num_id:
            counts[props.num_id] += 1
    return counts


def _observe_lists(
    obs: DocObservations, numbering: Numbering, used_lists: Counter
) -> None:
    """The house bullet list and the house numbered list, by level.

    Observed as shapes rather than ids, and only the most-used list of each
    kind: a document's incidental fourth bullet variant is not its format.
    """
    best: dict[str, tuple[int, int]] = {}
    for num_id, count in used_lists.items():
        level = numbering.level(num_id, 0)
        if level is None:
            continue
        kind = "bullet" if level.is_bullet else "numbered"
        if count > best.get(kind, (0, 0))[1]:
            best[kind] = (num_id, count)

    for kind, (num_id, _) in sorted(best.items()):
        for ilvl, level in sorted(numbering.levels(num_id).items()):
            if level.tentative:
                continue
            base = ptr("lists", kind, f"level{ilvl}")
            obs.add(f"{base}/num_fmt", level.num_fmt)
            obs.add(f"{base}/marker", level.marker)
            obs.add(f"{base}/indent_left", level.indent_left)
            obs.add(f"{base}/indent_hanging", level.indent_hanging)
            obs.add(f"{base}/suffix", level.suffix or "tab")
            obs.add(f"{base}/start", level.start)
        obs.add(ptr("lists", kind, "levels"), len(numbering.levels(num_id)))


# -- headers and footers --------------------------------------------------


def _observe_hdrftr(
    obs: DocObservations, pkg: OpcPackage, sections: SectionModel
) -> None:
    """Header/footer content of the primary section, keyed by slot.

    Text is normalised before it is observed: an NBSP or a smart quote makes
    two identical headers look different, and boilerplate detection is exactly
    the thing that must not be defeated by an invisible character.
    """
    from ..oox.walk import Walker as _Walker  # local: avoids a cycle at import

    for index in range(len(sections)):
        for ref in sections.slots(index):
            if not ref.part or ref.defined_in != index:
                continue
            text = " ".join(
                block.text
                for block in _Walker(pkg)._walk(
                    pkg.element(ref.part), ref.kind,
                    _hdr_context(ref.part, ref.kind),
                )
                if block.is_paragraph and block.text.strip()
            )
            base = ptr("hdrftr", ref.kind, ref.type)
            obs.add(f"{base}/text", match_key(text))
            obs.add(f"{base}/present", True)


def _hdr_context(part: str, kind: str):
    from ..oox.walk import Context

    return Context(part=part, kind=kind)
