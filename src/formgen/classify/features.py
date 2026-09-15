"""Measuring a block, and measuring the document it lives in.

Every relative heuristic in the classifier is relative to **this document's**
own body text, never to an absolute size. A memo set in 10pt Arial and a report
set in 12pt Times both have headings; what makes a paragraph a heading is that
it is bigger than the body *around it*. Comparing against an absolute 11pt
would classify the whole memo as small print.

So the document's modal body signature is computed first, from the paragraphs
that look like prose -- several of them, unstyled or body-styled, with ordinary
sentence punctuation -- and everything else is expressed as a ratio to it.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from lxml import etree

from ..oox.numbering import NumRef, Numbering, numbering_of
from ..oox.props import ParaProps, RunProps
from ..oox.sections import SectionModel
from ..oox.settings import Settings
from ..oox.styles import StyleGraph, normalize_style_name
from ..oox.theme import Theme
from ..oox.values import FontSize
from ..oox.walk import Block, Walker, field_instructions, match_key
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage

SEQ = re.compile(r"\bSEQ\s+(\w+)", re.I)
CAPTION_LEAD = re.compile(r"^\s*(figure|fig\.?|table|exhibit|chart|plate)\s*[\d\w]", re.I)
TERMINAL = ".!?:;…"


@dataclass
class BlockFeatures:
    """One block, with everything a rule might ask about already resolved."""

    block: Block
    style_id: str | None = None
    style_name: str = ""
    font: str | None = None      # theme-resolved typeface
    run: RunProps = field(default_factory=RunProps)
    para: ParaProps = field(default_factory=ParaProps)
    outline_level: int | None = None
    numbering: NumRef | None = None
    text: str = ""
    words: int = 0
    ends_with_terminal: bool = False
    all_caps: bool = False
    size_ratio: float | None = None
    seq_kind: str | None = None          # "Figure" / "Table" from a SEQ field
    caption_lead: bool = False
    follows_graphic: bool = False
    precedes_graphic: bool = False
    has_drawing: bool = False
    sdt_tag: str | None = None
    is_empty: bool = False
    body_ordinal: int | None = None   # position among non-empty body paragraphs
    signature: tuple = ()

    @property
    def path(self) -> str:
        return self.block.path

    @property
    def is_paragraph(self) -> bool:
        return self.block.is_paragraph


@dataclass
class DocumentContext:
    """Everything resolved once per document."""

    pkg: OpcPackage
    styles: StyleGraph
    numbering: Numbering
    sections: SectionModel
    settings: Settings
    blocks: list[Block] = field(default_factory=list)
    features: list[BlockFeatures] = field(default_factory=list)
    modal_size: FontSize | None = None
    modal_font: str | None = None
    modal_style: str = ""
    # Share of the document's paragraphs carrying each style name. A style
    # that covers almost everything discriminates nothing, which is precisely
    # the situation in a converted document where every paragraph is Normal.
    style_share: dict[str, float] = field(default_factory=dict)

    def by_path(self, path: str) -> BlockFeatures | None:
        return self._index.get(path)

    def __post_init__(self) -> None:
        self._index = {f.path: f for f in self.features}


def _part_root(pkg: OpcPackage, reltype: str) -> etree._Element | None:
    name = pkg.related(RT[reltype])
    return pkg.element(name) if name and name in pkg else None


def build_context(pkg: OpcPackage) -> DocumentContext:
    theme = Theme.parse(_part_root(pkg, "theme"))
    styles_root = _part_root(pkg, "styles")
    styles = (StyleGraph.parse(styles_root, theme) if styles_root is not None
              else StyleGraph({}, RunProps(), ParaProps(), theme))
    numbering = Numbering.parse(_part_root(pkg, "numbering"), styles)
    settings = Settings.parse(_part_root(pkg, "settings"))
    sections = SectionModel.from_package(pkg, settings)
    blocks = Walker(pkg).blocks()

    features = [_measure(block, styles, numbering) for block in blocks]
    _add_adjacency(blocks, features)
    ordinal = 0
    for feature in features:
        if feature.is_paragraph and not feature.is_empty \
                and feature.block.context.kind == "body":
            ordinal += 1
            feature.body_ordinal = ordinal
    modal_size, modal_font, modal_style = _modal_body(features)
    share = _style_share(features)
    for feature in features:
        if modal_size and feature.run.size:
            feature.size_ratio = feature.run.size.points / modal_size.points
    return DocumentContext(
        pkg=pkg, styles=styles, numbering=numbering, sections=sections,
        settings=settings, blocks=blocks, features=features,
        modal_size=modal_size, modal_font=modal_font, modal_style=modal_style,
        style_share=share,
    )


def _measure(block: Block, styles: StyleGraph, numbering: Numbering) -> BlockFeatures:
    features = BlockFeatures(block=block)
    if not block.is_paragraph:
        features.text = block.text
        features.has_drawing = _has_graphic(block.element)
        return features

    style_id = styles.para_style_or_default(block.style_id)
    style = styles.by_id(style_id)
    direct_para = ParaProps.parse(block.element.find(qn("w:pPr")))
    first_run = _first_run(block.element)
    direct_run = RunProps.parse(
        first_run.find(qn("w:rPr")) if first_run is not None else None
    )

    text = block.text
    words = text.split()
    features.style_id = style_id
    features.style_name = normalize_style_name(style.name) if style else ""
    features.run = styles.effective_for_run(style_id, direct_run.style_id, direct_run)
    features.para = styles.effective_for_para(style_id, direct_para)
    # Theme indirection: w:asciiTheme beats w:ascii, so the raw attribute is
    # not the typeface Word shows.
    features.font = styles.theme.resolve_font(
        features.run.font_ascii, features.run.font_ascii_theme
    )
    features.outline_level = (
        None if features.para.outline_level is None or features.para.outline_level >= 9
        else features.para.outline_level
    )
    features.numbering = numbering_of(styles, numbering, block.style_id, direct_para)
    features.text = text
    features.words = len(words)
    features.ends_with_terminal = bool(text.strip()) and text.strip()[-1] in TERMINAL
    letters = [c for c in text if c.isalpha()]
    features.all_caps = bool(letters) and all(c.isupper() for c in letters)
    features.caption_lead = bool(CAPTION_LEAD.match(text))
    features.seq_kind = _seq_kind(block.element)
    features.has_drawing = _has_graphic(block.element)
    features.sdt_tag = block.context.sdt_tag
    features.is_empty = not text.strip()
    features.signature = _signature(features)
    return features


def _first_run(paragraph: etree._Element) -> etree._Element | None:
    for run in paragraph.iter(qn("w:r")):
        parent = run.getparent()
        if parent is not None and parent.tag == qn("w:pPr"):
            continue          # the paragraph mark, not any text
        return run
    return None


def _has_graphic(element: etree._Element) -> bool:
    for tag in ("w:drawing", "w:pict", "w:object", "a:blip"):
        if element.find(f".//{qn(tag)}") is not None:
            return True
    return False


def _seq_kind(paragraph: etree._Element) -> str | None:
    """Read a SEQ field, reassembling instructions split across runs.

    Word routinely splits a field instruction mid-word -- ``SEQ Fig`` +
    ``ure \\* ARABIC`` -- so matching run by run finds nothing in exactly the
    documents that have real auto-numbered captions.
    """
    for instruction in field_instructions(paragraph):
        match = SEQ.search(instruction)
        if match:
            return match.group(1).title()
    return None


def _signature(features: BlockFeatures) -> tuple:
    """What "formatted the same way" means, for the consistency pass."""
    run, para = features.run, features.para
    return (
        features.style_id,
        run.size.half_points if run.size else None,
        bool(run.bold), bool(run.italic), bool(run.caps),
        para.alignment,
        para.indent_left.twips if para.indent_left else None,
        features.outline_level,
        features.numbering.ilvl if features.numbering else None,
        bool(features.numbering and features.numbering.is_bullet),
    )


def _add_adjacency(blocks: Sequence[Block], features: Sequence[BlockFeatures]) -> None:
    """A caption is recognised largely by what it sits next to."""
    def _skippable(other: BlockFeatures) -> bool:
        # Blank spacer paragraphs sit between a figure and its caption all the
        # time and must not break the adjacency. A paragraph holding only an
        # image is ALSO textless -- and is the very thing we are looking for --
        # so emptiness alone is the wrong test.
        return other.is_paragraph and other.is_empty and not other.has_drawing

    def _is_graphic(other: BlockFeatures) -> bool:
        return other.has_drawing or other.block.kind == "tbl"

    for i, feature in enumerate(features):
        for j in range(i - 1, -1, -1):
            if features[j].block.context.part != feature.block.context.part:
                break
            if _skippable(features[j]):
                continue
            feature.follows_graphic = _is_graphic(features[j])
            break
        for j in range(i + 1, len(features)):
            if features[j].block.context.part != feature.block.context.part:
                break
            if _skippable(features[j]):
                continue
            feature.precedes_graphic = _is_graphic(features[j])
            break


def _modal_body(features: Sequence[BlockFeatures]) -> tuple[FontSize | None, str | None, str]:
    """The document's own body signature: prose paragraphs, by weight of text.

    Weighted by word count rather than by paragraph count, because a document
    with thirty one-line table cells and eight real paragraphs of prose should
    take its body size from the prose.
    """
    sizes: Counter = Counter()
    fonts: Counter = Counter()
    styles: Counter = Counter()
    for feature in features:
        if not feature.is_paragraph or feature.is_empty:
            continue
        if feature.words < 8 or not feature.ends_with_terminal:
            continue
        if feature.outline_level is not None or feature.numbering:
            continue
        weight = feature.words
        if feature.run.size:
            sizes[feature.run.size] += weight
        if feature.font:
            fonts[feature.font] += weight
        if feature.style_name:
            styles[feature.style_name] += weight
    if not sizes:
        # No prose at all -- a cover sheet, a form. Fall back to every
        # non-empty paragraph rather than reporting no body size, since every
        # ratio downstream would otherwise be undefined.
        for feature in features:
            if feature.is_paragraph and not feature.is_empty and feature.run.size:
                sizes[feature.run.size] += 1
                if feature.style_name:
                    styles[feature.style_name] += 1
    return (
        _modal(sizes), _modal(fonts), _modal(styles) or "",
    )


def _style_share(features: Sequence[BlockFeatures]) -> dict[str, float]:
    counts: Counter = Counter()
    for feature in features:
        if feature.is_paragraph and not feature.is_empty:
            counts[feature.style_name] += 1
    total = sum(counts.values())
    return {name: n / total for name, n in counts.items()} if total else {}


def _modal(counter: Counter):
    if not counter:
        return None
    return min(counter.items(), key=lambda kv: (-kv[1], repr(kv[0])))[0]


def text_key(text: str) -> str:
    return match_key(text)
