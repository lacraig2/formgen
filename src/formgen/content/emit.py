"""ContentDoc -> .docx, starting from the profile's donor.

Generation follows the same principle as reformatting: **start from the donor
package and add content**, rather than synthesising a package from JSON. The
styles, theme, numbering, headers, footers, compat settings and embedded fonts
are already right because they were never rebuilt, so a generated document
lints clean against the profile it came from -- the property that justified
writing our own emitter instead of shelling out to Pandoc.

Two decisions worth stating plainly:

**Fields are emitted, never computed.** A table of contents, a `SEQ` figure
number, a `PAGEREF` -- all need a layout engine, and we do not have one. So
the field goes in with `w:dirty="true"` and a visible instruction to press F9,
and `settings.xml` asks Word to update on open. Faking a page number is worse
than omitting it, because a wrong one is believed.

**Every label becomes a bookmark, and every bookmark name is sanitised.**
Word bookmark names admit letters, digits and underscores only, so `fig:panel`
becomes `fig_panel`; the mapping is kept so cross-references resolve.
"""

from __future__ import annotations

import posixpath
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from ..oox.sections import SectionModel
from ..oox.styles import StyleGraph, normalize_style_name
from ..oox.values import Length
from ..opc.ns import NS, RT, qn
from ..opc.package import OpcPackage
from .ast import (
    BlockQuote, Code, CodeBlock, ContentDoc, CrossReference, Directive,
    Emphasis, Figure, Footnote, FootnoteReference, Heading, Image, Inline,
    LineBreak, Link, ListBlock, ListItem, Math, MathBlock, PageBreak,
    Paragraph, Table, TableOfContents, Text, ThematicBreak,
)
from .omml import to_omml

EMU_PER_PIXEL_AT_96 = 9525
DEFAULT_IMAGE_DPI = 96

# role -> the style NAME to look for in the donor. Names, not ids: a built-in
# style's id is localised and its name is not.
ROLE_STYLE_NAMES = {
    "title": ("title",),
    "body": ("body text", "normal"),
    "caption": ("caption",),
    "quote": ("quote", "block text"),
    "list_bullet": ("list bullet", "list paragraph"),
    "list_number": ("list number", "list paragraph"),
    "code": ("html preformatted", "plain text", "no spacing", "body text"),
    "toc_heading": ("toc heading", "heading 1"),
    "footnote": ("footnote text", "body text", "normal"),
}

_BOOKMARK_SAFE = re.compile(r"[^A-Za-z0-9_]")
MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".emf": "image/x-emf", ".wmf": "image/x-wmf",
    ".svg": "image/svg+xml",
}


@dataclass
class EmitReport:
    paragraphs: int = 0
    figures: int = 0
    tables: int = 0
    footnotes: int = 0
    fields: int = 0
    equations: int = 0
    filled: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{self.paragraphs} paragraphs"]
        for count, name in ((self.figures, "figure"), (self.tables, "table"),
                            (self.footnotes, "footnote"),
                            (self.equations, "equation"), (self.fields, "field")):
            if count:
                bits.append(f"{count} {name}{'s' if count != 1 else ''}")
        return ", ".join(bits)


def bookmark_name(label: str) -> str:
    """Word bookmark names allow letters, digits and underscore only."""
    safe = _BOOKMARK_SAFE.sub("_", label).lstrip("_") or "ref"
    if not safe[0].isalpha():
        safe = "b" + safe
    return safe[:40]


class Emitter:
    """Writes a ContentDoc into a package cloned from the donor."""

    def __init__(self, pkg: OpcPackage, source_dir: Path | None = None,
                 table_style: str | None = None):
        self.pkg = pkg
        self.source_dir = Path(source_dir or ".")
        self.report = EmitReport()
        self.styles = self._graph()
        self.sections = SectionModel.from_package(pkg)
        self.table_style = table_style
        self._bookmark_id = 1000
        self._drawing_id = 1000
        self._media: dict[str, str] = {}
        self._counters = {"Figure": 0, "Table": 0}
        self._labels: dict[str, str] = {}

    # -- setup -----------------------------------------------------------

    def _graph(self) -> StyleGraph:
        part = self.pkg.related(RT["styles"])
        if part and part in self.pkg:
            return StyleGraph.parse(self.pkg.element(part))
        from ..oox.props import ParaProps, RunProps

        return StyleGraph({}, RunProps(), ParaProps())

    def style_for(self, role: str) -> str | None:
        if role.startswith("heading") and role[7:].isdigit():
            names: tuple[str, ...] = (f"heading {int(role[7:])}",)
        else:
            names = ROLE_STYLE_NAMES.get(role, ())
        for name in names:
            style = self.styles.by_name(name)
            if style is not None:
                return style.style_id
        if role in ("body", "footnote", "code"):
            default = self.styles.default_paragraph_style()
            return default.style_id if default else None
        self.report.warnings.append(
            f"the profile defines no style for {role!r}; "
            "those paragraphs use the default style"
        )
        default = self.styles.default_paragraph_style()
        return default.style_id if default else None

    # -- the entry point -------------------------------------------------

    def emit(self, doc: ContentDoc, keep_cover: bool = True) -> EmitReport:
        body = self.pkg.edit(self.pkg.main_document).find(qn("w:body"))
        if body is None:
            raise ValueError("the donor has no w:body")
        sect = self._detach_body(body, keep_cover)

        self._labels = {
            label: bookmark_name(label) for label in doc.labels()
        }
        for block in doc.blocks:
            for element in self._block(block, doc):
                body.append(element)
        self._emit_footnotes(doc)
        self._fill_placeholders(doc, body)

        # The body-level w:sectPr must be the last child, and Word repairs a
        # file whose body ends in a table rather than a paragraph.
        if len(body) and body[-1].tag == qn("w:tbl"):
            body.append(self._empty_paragraph())
        if sect is not None:
            body.append(sect)
        return self.report

    def _detach_body(self, body: etree._Element, keep_cover: bool) -> etree._Element | None:
        """Empty the donor's body, optionally keeping its cover region.

        The cover is everything before the donor's first heading: the title
        block, the distribution statement, the fields a template carries. It
        is the part of a template people actually want kept, and the part
        that is hardest to express in Markdown.
        """
        sect = None
        children = [c for c in body if isinstance(c.tag, str)]
        if children and children[-1].tag == qn("w:sectPr"):
            sect = children.pop()

        cutoff = len(children) if keep_cover else 0
        if keep_cover:
            for index, child in enumerate(children):
                if child.tag == qn("w:p") and self._is_heading(child):
                    cutoff = index
                    break
            else:
                # No heading at all: the donor is one long body, and keeping
                # all of it would duplicate the exemplar's prose under the
                # new content. Keep nothing.
                cutoff = 0
        for child in children[cutoff:]:
            body.remove(child)
        return sect

    def _is_heading(self, paragraph: etree._Element) -> bool:
        style = paragraph.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        if style is None:
            return False
        level = self.styles.outline_level_of(style.get(qn("w:val")))
        return level is not None

    # -- blocks ----------------------------------------------------------

    def _block(self, block, doc: ContentDoc) -> list[etree._Element]:
        if isinstance(block, Heading):
            return [self._heading(block, doc)]
        if isinstance(block, Paragraph):
            return [self._paragraph(block.children, self.style_for(block.role),
                                    doc, label=block.label)]
        if isinstance(block, ListBlock):
            return self._list(block, doc)
        if isinstance(block, Table):
            return self._table(block, doc)
        if isinstance(block, Figure):
            return self._figure(block, doc)
        if isinstance(block, CodeBlock):
            return self._code(block)
        if isinstance(block, BlockQuote):
            out: list[etree._Element] = []
            for inner in block.blocks:
                out.extend(self._block(inner, doc))
            for element in out:
                self._set_style(element, self.style_for("quote"))
            return out
        if isinstance(block, Directive):
            return self._directive(block, doc)
        if isinstance(block, MathBlock):
            return [self._math_block(block)]
        if isinstance(block, TableOfContents):
            return self._toc(block)
        if isinstance(block, PageBreak):
            paragraph = self._empty_paragraph()
            run = etree.SubElement(paragraph, qn("w:r"))
            brk = etree.SubElement(run, qn("w:br"))
            brk.set(qn("w:type"), "page")
            return [paragraph]
        if isinstance(block, ThematicBreak):
            paragraph = self._empty_paragraph()
            ppr = paragraph.find(qn("w:pPr")) or etree.SubElement(
                paragraph, qn("w:pPr")
            )
            borders = etree.SubElement(ppr, qn("w:pBdr"))
            bottom = etree.SubElement(borders, qn("w:bottom"))
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "auto")
            return [paragraph]
        if isinstance(block, Footnote):
            return []
        self.report.warnings.append(
            f"{type(block).__name__} has no .docx representation and was dropped"
        )
        return []

    def _heading(self, block: Heading, doc: ContentDoc) -> etree._Element:
        level = max(1, min(9, block.level))
        return self._paragraph(
            block.children, self.style_for(f"heading{level}"), doc,
            label=block.label,
        )

    def _paragraph(self, inlines, style_id, doc, label: str = "",
                   numbering: tuple[int, int] | None = None) -> etree._Element:
        paragraph = etree.Element(qn("w:p"))
        ppr = etree.SubElement(paragraph, qn("w:pPr"))
        if style_id:
            style = etree.SubElement(ppr, qn("w:pStyle"))
            style.set(qn("w:val"), style_id)
        if numbering is not None:
            num_pr = etree.SubElement(ppr, qn("w:numPr"))
            ilvl = etree.SubElement(num_pr, qn("w:ilvl"))
            ilvl.set(qn("w:val"), str(numbering[1]))
            num_id = etree.SubElement(num_pr, qn("w:numId"))
            num_id.set(qn("w:val"), str(numbering[0]))
        if label:
            start, end = self._bookmark(self._labels.get(label, bookmark_name(label)))
            paragraph.append(start)
            for element in self._inlines(inlines, doc):
                paragraph.append(element)
            paragraph.append(end)
        else:
            for element in self._inlines(inlines, doc):
                paragraph.append(element)
        self.report.paragraphs += 1
        return paragraph

    def _empty_paragraph(self, style_id: str | None = None) -> etree._Element:
        paragraph = etree.Element(qn("w:p"))
        if style_id:
            ppr = etree.SubElement(paragraph, qn("w:pPr"))
            style = etree.SubElement(ppr, qn("w:pStyle"))
            style.set(qn("w:val"), style_id)
        return paragraph

    def _set_style(self, paragraph: etree._Element, style_id: str | None) -> None:
        if paragraph.tag != qn("w:p") or not style_id:
            return
        ppr = paragraph.find(qn("w:pPr"))
        if ppr is None:
            ppr = etree.Element(qn("w:pPr"))
            paragraph.insert(0, ppr)
        style = ppr.find(qn("w:pStyle"))
        if style is None:
            style = etree.Element(qn("w:pStyle"))
            ppr.insert(0, style)
        style.set(qn("w:val"), style_id)

    # -- inlines ---------------------------------------------------------

    def _inlines(self, inlines: list[Inline], doc: ContentDoc,
                 rpr: str | None = None) -> list[etree._Element]:
        out: list[etree._Element] = []
        for inline in inlines:
            out.extend(self._inline(inline, doc, rpr))
        return out

    def _inline(self, inline: Inline, doc: ContentDoc,
                rpr: str | None = None) -> list[etree._Element]:
        if isinstance(inline, Text):
            return [self._run(inline.value, rpr)]
        if isinstance(inline, Emphasis):
            marks = ("w:b",) if inline.strong else ("w:i",)
            return [
                self._decorate(element, marks)
                for element in self._inlines(inline.children, doc, rpr)
            ]
        if isinstance(inline, Code):
            run = self._run(inline.value, rpr)
            fonts = etree.SubElement(self._rpr(run), qn("w:rFonts"))
            fonts.set(qn("w:ascii"), "Consolas")
            fonts.set(qn("w:hAnsi"), "Consolas")
            return [run]
        if isinstance(inline, Link):
            return self._link(inline, doc)
        if isinstance(inline, CrossReference):
            return self._cross_reference(inline, doc)
        if isinstance(inline, FootnoteReference):
            return self._footnote_reference(inline, doc)
        if isinstance(inline, Math):
            conversion = to_omml(inline.tex)
            self.report.warnings.extend(conversion.warnings)
            self.report.equations += 1
            return [conversion.element]
        if isinstance(inline, LineBreak):
            run = etree.Element(qn("w:r"))
            etree.SubElement(run, qn("w:br"))
            return [run]
        if isinstance(inline, Image):
            return self._inline_image(inline)
        return []

    def _run(self, text: str, rpr: str | None = None) -> etree._Element:
        run = etree.Element(qn("w:r"))
        if rpr:
            properties = etree.SubElement(run, qn("w:rPr"))
            style = etree.SubElement(properties, qn("w:rStyle"))
            style.set(qn("w:val"), rpr)
        node = etree.SubElement(run, qn("w:t"))
        node.set(qn("xml:space"), "preserve")
        node.text = text
        return run

    def _rpr(self, run: etree._Element) -> etree._Element:
        properties = run.find(qn("w:rPr"))
        if properties is None:
            properties = etree.Element(qn("w:rPr"))
            run.insert(0, properties)
        return properties

    def _decorate(self, element: etree._Element, marks: tuple[str, ...]):
        if element.tag != qn("w:r"):
            return element
        properties = self._rpr(element)
        for mark in marks:
            if properties.find(qn(mark)) is None:
                node = etree.SubElement(properties, qn(mark))
                # Always explicit: a bare <w:b/> depends on the reader
                # implementing toggle inheritance correctly, and readers
                # disagree about that.
                node.set(qn("w:val"), "true")
        return element

    def _link(self, inline: Link, doc: ContentDoc) -> list[etree._Element]:
        if inline.href.startswith("#"):
            return self._cross_reference(
                CrossReference(label=inline.href[1:]), doc, inline.children
            )
        rid = self.pkg.touch_rels(self.pkg.main_document).add(
            RT["hyperlink"], inline.href, external=True
        )
        link = etree.Element(qn("w:hyperlink"))
        link.set(qn("r:id"), rid)
        style = self.styles.by_name("Hyperlink", "character")
        for element in self._inlines(
            inline.children or [Text(inline.href)], doc,
            rpr=style.style_id if style else None,
        ):
            link.append(element)
        return [link]

    # -- fields ----------------------------------------------------------

    def _field(self, instruction: str, result: str,
               dirty: bool = True) -> list[etree._Element]:
        """A complex field, emitted unpopulated on purpose.

        `w:dirty` asks Word to recompute it on open. The result text is what
        a reader sees until then, so it says what to do rather than showing a
        plausible-looking wrong number.
        """
        self.report.fields += 1
        begin_run = etree.Element(qn("w:r"))
        begin = etree.SubElement(begin_run, qn("w:fldChar"))
        begin.set(qn("w:fldCharType"), "begin")
        if dirty:
            begin.set(qn("w:dirty"), "true")

        instruction_run = etree.Element(qn("w:r"))
        node = etree.SubElement(instruction_run, qn("w:instrText"))
        node.set(qn("xml:space"), "preserve")
        node.text = instruction

        separate_run = etree.Element(qn("w:r"))
        separate = etree.SubElement(separate_run, qn("w:fldChar"))
        separate.set(qn("w:fldCharType"), "separate")

        end_run = etree.Element(qn("w:r"))
        end = etree.SubElement(end_run, qn("w:fldChar"))
        end.set(qn("w:fldCharType"), "end")

        return [begin_run, instruction_run, separate_run, self._run(result), end_run]

    def _cross_reference(self, inline: CrossReference, doc: ContentDoc,
                         children: list[Inline] | None = None) -> list[etree._Element]:
        target = self._labels.get(inline.label)
        if target is None:
            self.report.warnings.append(
                f"[](#{inline.label}) points at a label nothing defines; "
                "it was emitted as plain text"
            )
            return [self._run(inline.label)]
        kind = doc.labels().get(inline.label, "")
        shown = {"figure": "Figure", "table": "Table"}.get(kind, "")
        result = f"{shown} ?" if shown else "?"
        return self._field(f" REF {target} \\h ", result)

    def _sequence(self, kind: str) -> list[etree._Element]:
        self._counters[kind] = self._counters.get(kind, 0) + 1
        return self._field(f" SEQ {kind} \\* ARABIC ", str(self._counters[kind]))

    def _toc(self, block: TableOfContents) -> list[etree._Element]:
        low, high = block.levels
        paragraph = self._empty_paragraph(self.style_for("body"))
        for element in self._field(
            f' TOC \\o "{low}-{high}" \\h \\z \\u ',
            "Press F9 in Word to build the table of contents.",
        ):
            paragraph.append(element)
        self.report.paragraphs += 1
        return [paragraph]

    # -- footnotes -------------------------------------------------------

    def _footnote_reference(self, inline: FootnoteReference,
                            doc: ContentDoc) -> list[etree._Element]:
        if inline.label not in doc.footnotes:
            self.report.warnings.append(
                f"[^{inline.label}] has no definition and was dropped"
            )
            return []
        number = self._footnote_numbers.setdefault(
            inline.label, len(self._footnote_numbers) + self._footnote_base
        )
        run = etree.Element(qn("w:r"))
        style = self.styles.by_name("Footnote Reference", "character")
        if style is not None:
            properties = etree.SubElement(run, qn("w:rPr"))
            reference_style = etree.SubElement(properties, qn("w:rStyle"))
            reference_style.set(qn("w:val"), style.style_id)
        reference = etree.SubElement(run, qn("w:footnoteReference"))
        reference.set(qn("w:id"), str(number))
        return [run]

    _footnote_numbers: dict[str, int]
    _footnote_base: int

    def _emit_footnotes(self, doc: ContentDoc) -> None:
        if not getattr(self, "_footnote_numbers", None):
            return
        part = self._ensure_footnotes_part()
        root = self.pkg.edit(part)
        style_id = self.style_for("footnote")
        for label, number in sorted(self._footnote_numbers.items(),
                                    key=lambda kv: kv[1]):
            note = etree.SubElement(root, qn("w:footnote"))
            note.set(qn("w:id"), str(number))
            blocks = doc.footnotes[label].blocks or [Paragraph()]
            first = True
            for block in blocks:
                for element in self._block(block, doc):
                    self._set_style(element, style_id)
                    if first and element.tag == qn("w:p"):
                        mark = etree.Element(qn("w:r"))
                        etree.SubElement(mark, qn("w:footnoteRef"))
                        ppr = element.find(qn("w:pPr"))
                        element.insert(1 if ppr is not None else 0, mark)
                        first = False
                    note.append(element)
            self.report.footnotes += 1

    def _ensure_footnotes_part(self) -> str:
        """Create footnotes.xml if the donor has none.

        Dropping the author's footnotes because the template happened never
        to use one would be losing content over a detail they cannot see. The
        two separator notes are required: Word draws the line above the
        footnote area from them, and a footnotes part without them renders
        without the rule.
        """
        part = self.pkg.related(RT["footnotes"])
        if part and part in self.pkg:
            return part
        from ..opc.content_types import CT

        blob = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<w:footnotes xmlns:w="{NS["w"]}">'
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr>'
            '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            "<w:r><w:separator/></w:r></w:p></w:footnote>"
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr>'
            '<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            "<w:r><w:continuationSeparator/></w:r></w:p></w:footnote>"
            "</w:footnotes>"
        ).encode()
        name = posixpath.join(
            posixpath.dirname(self.pkg.main_document), "footnotes.xml"
        )
        self.pkg.add_part(name, blob, CT["footnotes"])
        self.pkg.relate(RT["footnotes"], name, self.pkg.main_document)
        return name

    def _prepare_footnotes(self, doc: ContentDoc) -> None:
        """Number footnotes above whatever the donor's separators use.

        Word's separator notes are conventionally -1 and 0, but that is a
        habit rather than a rule: other producers number the first real note
        0. Starting above every id actually present is the only safe choice.
        """
        self._footnote_numbers = {}
        self._footnote_base = 1
        part = self.pkg.related(RT["footnotes"])
        if part and part in self.pkg:
            ids = [
                int(raw) for note in self.pkg.element(part).findall(qn("w:footnote"))
                if (raw := note.get(qn("w:id"))) and raw.lstrip("-").isdigit()
            ]
            if ids:
                self._footnote_base = max(ids) + 1

    # -- lists -----------------------------------------------------------

    def _list(self, block: ListBlock, doc: ContentDoc,
              num_id: int | None = None, level: int = 0) -> list[etree._Element]:
        if num_id is None:
            num_id = self._new_list(block.ordered, block.start)
        if num_id is None:
            # No list definition to attach to: emit the items as ordinary
            # paragraphs rather than losing the text.
            self.report.warnings.append(
                "the profile defines no list numbering; list items were "
                "emitted as plain paragraphs"
            )
        style = self.style_for("list_number" if block.ordered else "list_bullet")
        out: list[etree._Element] = []
        for item in block.items:
            first = True
            for inner in item.blocks:
                if isinstance(inner, ListBlock):
                    out.extend(self._list(inner, doc, num_id, level + 1))
                    continue
                if isinstance(inner, Paragraph) and first and num_id is not None:
                    out.append(self._paragraph(
                        inner.children, style, doc, label=inner.label,
                        numbering=(num_id, level),
                    ))
                    first = False
                    continue
                elements = self._block(inner, doc)
                for element in elements:
                    self._set_style(element, style)
                out.extend(elements)
                first = False
        return out

    def _new_list(self, ordered: bool, start: int) -> int | None:
        """Mint a w:num of our own for this list.

        Reusing one w:num across two lists makes the second continue the
        first's numbering, which is the single most common list bug in
        generated documents.
        """
        part = self.pkg.related(RT["numbering"])
        if not part or part not in self.pkg:
            return None
        root = self.pkg.edit(part)
        from ..oox.numbering import Numbering

        numbering = Numbering.parse(root)
        chosen = None
        for num_id in sorted(numbering.instances):
            level = numbering.level(num_id, 0)
            if level is None:
                continue
            if level.is_bullet != ordered:
                chosen = numbering.instances[num_id].abstract_id
                break
        if chosen is None:
            return None

        used = {
            int(raw) for element in root.findall(qn("w:num"))
            if (raw := element.get(qn("w:numId"))) and raw.lstrip("-").isdigit()
        }
        new_id = max(used | {0}) + 1
        element = etree.SubElement(root, qn("w:num"))
        element.set(qn("w:numId"), str(new_id))
        target = etree.SubElement(element, qn("w:abstractNumId"))
        target.set(qn("w:val"), str(chosen))
        override = etree.SubElement(element, qn("w:lvlOverride"))
        override.set(qn("w:ilvl"), "0")
        start_override = etree.SubElement(override, qn("w:startOverride"))
        start_override.set(qn("w:val"), str(max(1, start)))
        return new_id

    # -- tables ----------------------------------------------------------

    def _table(self, block: Table, doc: ContentDoc) -> list[etree._Element]:
        out: list[etree._Element] = []
        if block.caption is not None:
            out.append(self._caption(block.caption, "Table", block.label, doc))
        table = etree.Element(qn("w:tbl"))
        properties = etree.SubElement(table, qn("w:tblPr"))
        style_name = self.table_style or "Table Grid"
        style = self.styles.by_name(style_name, "table")
        if style is not None:
            reference = etree.SubElement(properties, qn("w:tblStyle"))
            reference.set(qn("w:val"), style.style_id)
        width = etree.SubElement(properties, qn("w:tblW"))
        width.set(qn("w:w"), "5000")
        width.set(qn("w:type"), "pct")
        look = etree.SubElement(properties, qn("w:tblLook"))
        # Both spellings: the legacy bitmask AND the modern attributes. A
        # table style's conditional formatting is gated on this, and setting
        # only one half yields a table that looks unstyled.
        look.set(qn("w:val"), "04A0")
        for name, value in (("firstRow", "1"), ("lastRow", "0"),
                            ("firstColumn", "1"), ("lastColumn", "0"),
                            ("noHBand", "0"), ("noVBand", "1")):
            look.set(qn(f"w:{name}"), value)

        columns = max((len(row.cells) for row in block.rows), default=0)
        available = self._content_width()
        grid = etree.SubElement(table, qn("w:tblGrid"))
        for _ in range(columns):
            column = etree.SubElement(grid, qn("w:gridCol"))
            column.set(qn("w:w"), str(available // max(columns, 1)))

        for row in block.rows:
            table.append(self._row(row, columns, block, doc))
        out.append(table)
        # Word repairs a file in which two tables abut with no paragraph
        # between them, and one that ends the body with a table.
        out.append(self._empty_paragraph(self.style_for("body")))
        self.report.tables += 1
        return out

    def _row(self, row, columns: int, block: Table, doc: ContentDoc):
        element = etree.Element(qn("w:tr"))
        if row.header:
            properties = etree.SubElement(element, qn("w:trPr"))
            etree.SubElement(properties, qn("w:tblHeader"))
        for index in range(columns):
            cell = etree.SubElement(element, qn("w:tc"))
            properties = etree.SubElement(cell, qn("w:tcPr"))
            width = etree.SubElement(properties, qn("w:tcW"))
            width.set(qn("w:w"), "0")
            width.set(qn("w:type"), "auto")
            source = row.cells[index] if index < len(row.cells) else None
            alignment = (block.alignments[index]
                         if index < len(block.alignments) else "left")
            blocks = source.blocks if source is not None else []
            if not blocks:
                cell.append(self._empty_paragraph(self.style_for("body")))
                continue
            for inner in blocks:
                for produced in self._block(inner, doc):
                    self._set_style(produced, self.style_for("body"))
                    self._align(produced, alignment)
                    cell.append(produced)
        return element

    def _align(self, paragraph: etree._Element, alignment: str) -> None:
        if paragraph.tag != qn("w:p") or alignment == "left":
            return
        ppr = paragraph.find(qn("w:pPr"))
        if ppr is None:
            ppr = etree.Element(qn("w:pPr"))
            paragraph.insert(0, ppr)
        node = etree.SubElement(ppr, qn("w:jc"))
        node.set(qn("w:val"), "center" if alignment == "center" else "right")

    def _content_width(self) -> int:
        width = self.sections[0].content_width if len(self.sections) else None
        return width.twips if width else 9360

    # -- figures and captions --------------------------------------------

    def _figure(self, block: Figure, doc: ContentDoc) -> list[etree._Element]:
        out: list[etree._Element] = []
        paragraph = self._empty_paragraph(self.style_for("body"))
        jc = etree.SubElement(
            paragraph.find(qn("w:pPr")) if paragraph.find(qn("w:pPr")) is not None
            else etree.SubElement(paragraph, qn("w:pPr")),
            qn("w:jc"),
        )
        jc.set(qn("w:val"), "center")
        for element in self._inline_image(block.image):
            paragraph.append(element)
        out.append(paragraph)
        self.report.paragraphs += 1
        self.report.figures += 1
        if block.caption is not None:
            out.append(self._caption(block.caption, "Figure", block.label, doc))
        return out

    def _caption(self, caption: Paragraph, kind: str, label: str,
                 doc: ContentDoc) -> etree._Element:
        """A caption whose number is a SEQ field, not a typed-in digit.

        Typing the number is what makes a document wrong the moment a figure
        is inserted above it; the field renumbers itself and the REF fields
        that point at it follow.
        """
        paragraph = etree.Element(qn("w:p"))
        ppr = etree.SubElement(paragraph, qn("w:pPr"))
        style_id = self.style_for("caption")
        if style_id:
            style = etree.SubElement(ppr, qn("w:pStyle"))
            style.set(qn("w:val"), style_id)
        if label:
            start, end = self._bookmark(
                self._labels.get(label, bookmark_name(label))
            )
            paragraph.append(start)
        else:
            start = end = None

        text = caption.text()
        match = re.match(rf"^\s*{kind}\s*([0-9]+)?[.:]?\s*", text, re.I)
        remainder = text[match.end():] if match else text
        paragraph.append(self._run(f"{kind} "))
        for element in self._sequence(kind):
            paragraph.append(element)
        if remainder:
            paragraph.append(self._run(". " if not remainder.startswith(".") else ""))
            paragraph.append(self._run(remainder))
        if end is not None:
            paragraph.append(end)
        self.report.paragraphs += 1
        return paragraph

    def _inline_image(self, image: Image) -> list[etree._Element]:
        rid, size = self._add_image(image)
        if rid is None:
            return [self._run(f"[missing image: {image.src}]")]
        width, height = size
        self._drawing_id += 1
        drawing = etree.Element(qn("w:drawing"))
        inline = etree.SubElement(drawing, qn("wp:inline"))
        for name in ("distT", "distB", "distL", "distR"):
            inline.set(name, "0")
        extent = etree.SubElement(inline, qn("wp:extent"))
        extent.set("cx", str(width))
        extent.set("cy", str(height))
        effect = etree.SubElement(inline, qn("wp:effectExtent"))
        for name in ("l", "t", "r", "b"):
            effect.set(name, "0")
        doc_pr = etree.SubElement(inline, qn("wp:docPr"))
        # Document-wide unique ids: two drawings sharing one makes Word
        # renumber them on open, which shows up as a spurious diff.
        doc_pr.set("id", str(self._drawing_id))
        doc_pr.set("name", f"Picture {self._drawing_id}")
        if image.alt:
            doc_pr.set("descr", image.alt)
        frame = etree.SubElement(inline, qn("wp:cNvGraphicFramePr"))
        locks = etree.SubElement(frame, qn("a:graphicFrameLocks"))
        locks.set("noChangeAspect", "1")

        graphic = etree.SubElement(inline, qn("a:graphic"))
        data = etree.SubElement(graphic, qn("a:graphicData"))
        data.set("uri", NS["pic"])
        pic = etree.SubElement(data, qn("pic:pic"))
        nv = etree.SubElement(pic, qn("pic:nvPicPr"))
        c_nv = etree.SubElement(nv, qn("pic:cNvPr"))
        c_nv.set("id", "0")
        c_nv.set("name", posixpath.basename(image.src))
        if image.alt:
            c_nv.set("descr", image.alt)
        etree.SubElement(nv, qn("pic:cNvPicPr"))
        fill = etree.SubElement(pic, qn("pic:blipFill"))
        blip = etree.SubElement(fill, qn("a:blip"))
        blip.set(qn("r:embed"), rid)
        stretch = etree.SubElement(fill, qn("a:stretch"))
        etree.SubElement(stretch, qn("a:fillRect"))
        sp = etree.SubElement(pic, qn("pic:spPr"))
        xfrm = etree.SubElement(sp, qn("a:xfrm"))
        offset = etree.SubElement(xfrm, qn("a:off"))
        offset.set("x", "0")
        offset.set("y", "0")
        ext = etree.SubElement(xfrm, qn("a:ext"))
        ext.set("cx", str(width))
        ext.set("cy", str(height))
        geometry = etree.SubElement(sp, qn("a:prstGeom"))
        geometry.set("prst", "rect")
        etree.SubElement(geometry, qn("a:avLst"))

        run = etree.Element(qn("w:r"))
        run.append(drawing)
        return [run]

    def _add_image(self, image: Image) -> tuple[str | None, tuple[int, int]]:
        path = (self.source_dir / image.src).resolve()
        if not path.exists():
            self.report.warnings.append(f"image not found: {image.src}")
            return None, (0, 0)
        key = str(path)
        if key not in self._media:
            extension = path.suffix.lower()
            content_type = MEDIA_TYPES.get(extension)
            if content_type is None:
                self.report.warnings.append(
                    f"{image.src} is not an image type Word embeds; it was skipped"
                )
                return None, (0, 0)
            name = self.pkg.unique_partname(
                posixpath.join(posixpath.dirname(self.pkg.main_document),
                               f"media/image{{n}}{extension}")
            )
            self.pkg.add_part(name, path.read_bytes(), content_type)
            self._media[key] = self.pkg.relate(
                RT["image"], name, self.pkg.main_document
            )
        return self._media[key], self._image_size(path, image.width)

    def _image_size(self, path: Path, requested: str) -> tuple[int, int]:
        """Native size in EMU, scaled down to fit the text column.

        An image wider than the text column is the single most common way a
        generated document looks broken, and Word does not fix it for you.
        """
        pixels = (600, 400)
        dpi = (DEFAULT_IMAGE_DPI, DEFAULT_IMAGE_DPI)
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as handle:
                pixels = handle.size
                dpi = handle.info.get("dpi", dpi) or dpi
        except Exception:
            self.report.warnings.append(
                f"could not read the size of {path.name}; assumed 600x400"
            )
        x_dpi = float(dpi[0]) or DEFAULT_IMAGE_DPI
        y_dpi = float(dpi[1]) or DEFAULT_IMAGE_DPI
        width = int(pixels[0] / x_dpi * 914400)
        height = int(pixels[1] / y_dpi * 914400)

        limit = Length(self._content_width()).to_emu
        if requested.endswith("%") and requested[:-1].replace(".", "").isdigit():
            limit = int(limit * float(requested[:-1]) / 100)
        if width > limit and width:
            height = int(height * limit / width)
            width = limit
        return max(width, 1), max(height, 1)

    # -- misc ------------------------------------------------------------

    def _code(self, block: CodeBlock) -> list[etree._Element]:
        style_id = self.style_for("code")
        out = []
        for line in block.value.split("\n"):
            paragraph = self._empty_paragraph(style_id)
            run = self._run(line)
            fonts = etree.SubElement(self._rpr(run), qn("w:rFonts"))
            fonts.set(qn("w:ascii"), "Consolas")
            fonts.set(qn("w:hAnsi"), "Consolas")
            paragraph.append(run)
            out.append(paragraph)
            self.report.paragraphs += 1
        return out

    def _directive(self, block: Directive, doc: ContentDoc) -> list[etree._Element]:
        style = self.styles.by_name(block.name.replace("-", " "))
        style_id = style.style_id if style else None
        if style_id is None:
            self.report.warnings.append(
                f"::: {block.name} has no matching style in the profile; "
                "its content uses the body style"
            )
        out: list[etree._Element] = []
        for inner in block.blocks:
            elements = self._block(inner, doc)
            for element in elements:
                self._set_style(element, style_id or self.style_for("body"))
            out.extend(elements)
        return out

    def _math_block(self, block: MathBlock) -> etree._Element:
        conversion = to_omml(block.tex, display=True)
        self.report.warnings.extend(conversion.warnings)
        self.report.equations += 1
        paragraph = etree.Element(qn("w:p"))
        ppr = etree.SubElement(paragraph, qn("w:pPr"))
        style_id = self.style_for("body")
        if style_id:
            style = etree.SubElement(ppr, qn("w:pStyle"))
            style.set(qn("w:val"), style_id)
        paragraph.append(conversion.element)
        self.report.paragraphs += 1
        return paragraph

    def _bookmark(self, name: str) -> tuple[etree._Element, etree._Element]:
        self._bookmark_id += 1
        start = etree.Element(qn("w:bookmarkStart"))
        start.set(qn("w:id"), str(self._bookmark_id))
        start.set(qn("w:name"), name)
        end = etree.Element(qn("w:bookmarkEnd"))
        end.set(qn("w:id"), str(self._bookmark_id))
        return start, end

    def _fill_placeholders(self, doc: ContentDoc, body: etree._Element) -> None:
        """Write front-matter values into the donor's content controls.

        A bound control silently reverts anything written into it unless the
        custom XML is updated too, so the donor scrub strips bindings. Here we
        only have to replace the content and clear the "showing placeholder
        text" flag, or Word keeps rendering its grey prompt over our value.
        """
        for sdt in list(body.iter(qn("w:sdt"))):
            properties = sdt.find(qn("w:sdtPr"))
            content = sdt.find(qn("w:sdtContent"))
            if properties is None or content is None:
                continue
            tag = properties.find(qn("w:tag"))
            name = (tag.get(qn("w:val")) if tag is not None else "") or ""
            if not name.startswith("formgen."):
                continue
            key = name[len("formgen."):]
            for flag in properties.findall(qn("w:showingPlcHdr")):
                properties.remove(flag)
            if key in doc.meta:
                self._write_into(content, str(doc.meta[key]))
                self.report.filled.append(key)
                continue
            # No value for this field. The donor is a real report, so the
            # control still holds *that* report's value -- a number, a
            # customer name, a reviewer -- and leaving it is how one document
            # ships carrying another's identity. Clear it.
            self._write_into(content, "")
            self.report.cleared.append(key)

    def _write_into(self, content: etree._Element, value: str) -> None:
        """Replace a control's content, keeping the look the donor gave it.

        A control is block-level (its `w:sdtContent` holds paragraphs) or
        inline (it holds runs, inside a paragraph of its own). Writing a
        `w:p` into an inline one produces a file Word offers to repair, so
        the two cases are genuinely different and both have to be handled.

        The donor's own `w:rPr` is carried onto the new run. The house format
        may well set the report number in bold small caps, and that is part
        of the format, not part of the value it happened to hold.
        """
        host = content.find(qn("w:p"))
        inline = host is None
        if inline:
            host = content
        template = host.find(qn("w:r"))
        properties = deepcopy(template.find(qn("w:rPr"))) \
            if template is not None else None
        for run in host.findall(qn("w:r")):
            host.remove(run)
        run = self._run(value)
        if properties is not None:
            run.insert(0, properties)
        host.append(run)


def emit(
    doc: ContentDoc,
    template: Path,
    source_dir: Path | None = None,
    keep_cover: bool = True,
) -> tuple[OpcPackage, EmitReport]:
    """Render a ContentDoc into a package cloned from `template`."""
    pkg = OpcPackage.open(template)
    emitter = Emitter(pkg, source_dir=source_dir)
    emitter._prepare_footnotes(doc)
    emitter._footnote_numbers = {}
    for label in doc.footnote_labels_used():
        if label in doc.footnotes and label not in emitter._footnote_numbers:
            emitter._footnote_numbers[label] = (
                emitter._footnote_base + len(emitter._footnote_numbers)
            )
    report = emitter.emit(doc, keep_cover=keep_cover)
    return pkg, report
