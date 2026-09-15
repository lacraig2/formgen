"""`.docx` -> ContentDoc -> Markdown.

**This is for git-native workflows, not for reformatting.** Round-tripping a
foreign document through Markdown loses comments, tracked changes, embedded
objects and text boxes -- exactly what the graft exists to preserve -- so
`apply` must never be implemented as `extract` + `emit`. Saying so here rather
than in a design note, because the shortcut is tempting and the damage is
silent.

What it is for: keeping documents in version control, reviewing a change as a
diff, and giving `emit` a test oracle. ``extract(emit(md)) ~= md`` over a
fixture corpus is what polices both halves of the pair, and it is the reason
they were built together.

Roles come from the classifier rather than from `w:pStyle`, so extraction
works on a converted document where nothing is styled -- the same reason lint
does it that way.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from lxml import etree

from ..classify.features import build_context
from ..classify.rules import classify_document
from ..oox.walk import Block, Walker, field_instructions
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from .ast import (
    Code, CodeBlock, ContentDoc, CrossReference, Emphasis, Figure, Footnote,
    FootnoteReference, Heading, Image, Inline, LineBreak, Link, ListBlock,
    ListItem, Math, MathBlock, PageBreak, Paragraph, Table, TableCell,
    TableRow, TableOfContents, Text,
)
from .omml import to_tex

REF_FIELD = re.compile(r"\bREF\s+([A-Za-z0-9_]+)", re.I)
SEQ_FIELD = re.compile(r"\bSEQ\s+(\w+)", re.I)
TOC_FIELD = re.compile(r"\bTOC\b", re.I)
CAPTION_NUMBER = re.compile(r"^\s*(Figure|Table)\s*$", re.I)

_ESCAPE = re.compile(r"([\\`*_\[\]<>])")
_LINE_START = re.compile(r"^(\s*)([#>\-+]|\d+\.)\s")


def extract(pkg: OpcPackage, media_dir: Path | None = None,
            profile_styles: set[str] | None = None) -> ContentDoc:
    """Read a package into the AST."""
    ctx = build_context(pkg)
    roles = {
        path: c.role
        for path, c in classify_document(ctx, profile_styles or set(), set()).items()
    }
    doc = ContentDoc()
    reader = _Reader(pkg, ctx, roles, media_dir)
    doc.blocks = reader.body()
    doc.footnotes = reader.footnotes()
    doc.meta = reader.meta()
    doc.warnings = reader.warnings
    return doc


class _Reader:
    def __init__(self, pkg: OpcPackage, ctx, roles, media_dir: Path | None):
        self.pkg = pkg
        self.ctx = ctx
        self.roles = roles
        self.media_dir = media_dir
        self.warnings: list[str] = []
        self._bookmarks: dict[str, str] = {}
        self._footnote_texts: dict[str, list] = {}
        self._label_of: dict[str, str] = {}
        self._sequence_counts: dict[str, int] = {}
        self._title: str = ""

    # -- the body --------------------------------------------------------

    def body(self) -> list:
        blocks = [
            b for b in self.ctx.blocks
            if b.context.part == self.pkg.main_document
            and b.context.kind in ("body", "table")
            and not b.context.in_del
        ]
        top = [b for b in blocks if b.context.kind == "body"]
        out: list = []
        pending_list: ListBlock | None = None

        index = 0
        while index < len(top):
            block = top[index]
            if block.kind == "tbl":
                pending_list = None
                out.append(self._table(block))
                index += 1
                continue
            if not block.is_paragraph:
                index += 1
                continue

            role = self.roles.get(block.path, "body")
            if role == "empty":
                pending_list = None
                index += 1
                continue

            if role == "title" and not self._title:
                # The document's title belongs in the front matter, which is
                # where `new` reads it from -- emitting it as a heading would
                # make the round trip grow an extra level every pass.
                self._title = block.text.strip()
                index += 1
                continue

            feature = self.ctx.by_path(block.path)
            if role in ("list_bullet", "list_number") and feature is not None:
                ordered = role == "list_number"
                level = feature.numbering.ilvl if feature.numbering else 0
                if pending_list is None or pending_list.ordered != ordered:
                    pending_list = ListBlock(ordered=ordered)
                    out.append(pending_list)
                self._add_item(pending_list, block, level)
                index += 1
                continue
            pending_list = None

            if TOC_FIELD.search(" ".join(field_instructions(block.element))):
                out.append(TableOfContents())
                index += 1
                continue
            if self._is_page_break(block.element):
                out.append(PageBreak())
                index += 1
                continue

            math = self._only_math(block.element)
            if math is not None:
                out.append(MathBlock(tex=to_tex(math)))
                index += 1
                continue

            paragraph = self._paragraph(block, role)
            image = self._only_image(paragraph)
            if image is not None:
                figure = Figure(image=image, label=paragraph.label)
                out.append(figure)
                index += 1
                if index < len(top) and self.roles.get(top[index].path) == "caption":
                    caption = self._paragraph(top[index], "caption")
                    figure.caption = caption
                    figure.label = figure.label or caption.label
                    caption.label = ""
                    index += 1
                continue
            if role == "caption" and out and isinstance(out[-1], Table) \
                    and out[-1].caption is None:
                out[-1].caption = paragraph
                out[-1].label = out[-1].label or paragraph.label
                paragraph.label = ""
                index += 1
                continue
            out.append(paragraph)
            index += 1
        return out

    def _add_item(self, block: ListBlock, source: Block, level: int) -> None:
        paragraph = self._paragraph(source, "body")
        if level == 0 or not block.items:
            block.items.append(ListItem(blocks=[paragraph]))
            return
        parent = block.items[-1]
        for _ in range(level - 1):
            nested = [b for b in parent.blocks if isinstance(b, ListBlock)]
            if not nested or not nested[-1].items:
                break
            parent = nested[-1].items[-1]
        nested = [b for b in parent.blocks if isinstance(b, ListBlock)]
        if not nested:
            child = ListBlock(ordered=block.ordered, level=level)
            parent.blocks.append(child)
            nested = [child]
        nested[-1].items.append(ListItem(blocks=[paragraph]))

    def _paragraph(self, block: Block, role: str) -> Paragraph:
        children = self._inlines(block.element)
        label = self._label_in(block.element)
        if role == "title":
            return Heading(level=1, children=children, label=label)
        if role.startswith("heading") and role[7:].isdigit():
            return Heading(level=int(role[7:]), children=children, label=label)
        return Paragraph(children=children, role=role, label=label)

    def _only_image(self, paragraph) -> Image | None:
        if not isinstance(paragraph, Paragraph):
            return None
        images = [c for c in paragraph.children if isinstance(c, Image)]
        if len(images) != 1:
            return None
        if paragraph.text().strip():
            return None
        return images[0]

    def _only_math(self, paragraph: etree._Element) -> etree._Element | None:
        """A paragraph holding nothing but display maths.

        Testing for empty text is not enough: the walker reads `m:t` as text
        (it is text), so an equation paragraph is never textless.
        """
        content = [
            child for child in paragraph
            if isinstance(child.tag, str) and child.tag != qn("w:pPr")
        ]
        if len(content) == 1 and content[0].tag == qn("m:oMathPara"):
            return content[0]
        return None

    def _is_page_break(self, element: etree._Element) -> bool:
        for brk in element.findall(f".//{qn('w:br')}"):
            if brk.get(qn("w:type")) == "page":
                return True
        return False

    def _label_in(self, element: etree._Element) -> str:
        for start in element.findall(f".//{qn('w:bookmarkStart')}"):
            name = start.get(qn("w:name")) or ""
            # _GoBack is Word's own cursor memory, not an author's anchor.
            if name and not name.startswith("_"):
                return name
        return ""

    # -- inlines ---------------------------------------------------------

    def _inlines(self, element: etree._Element) -> list[Inline]:
        out: list[Inline] = []
        self._collect(element, out, in_field=False)
        return _merge_text(out)

    def _collect(self, parent: etree._Element, out: list[Inline],
                 in_field: bool) -> bool:
        """Walk one paragraph, turning fields and runs back into inlines.

        Field instructions are reassembled across runs before matching --
        Word splits ``SEQ Figure`` mid-word -- and the *result* of a field is
        skipped, because it is a cached rendering we must not treat as text
        the author wrote.
        """
        skipping = in_field
        for child in parent:
            if not isinstance(child.tag, str):
                continue
            tag = child.tag
            if tag == qn("w:hyperlink"):
                rid = child.get(qn("r:id"))
                href = self._href(rid) if rid else ""
                anchor = child.get(qn("w:anchor"))
                children: list[Inline] = []
                self._collect(child, children, in_field=False)
                if anchor:
                    out.append(CrossReference(label=self._label_for(anchor)))
                else:
                    out.append(Link(href=href, children=_merge_text(children)))
                continue
            if tag == qn("m:oMath"):
                out.append(Math(tex=to_tex(child)))
                continue
            if tag in (qn("w:ins"), qn("w:moveTo"), qn("w:sdt"),
                       qn("w:sdtContent"), qn("w:smartTag")):
                skipping = self._collect(child, out, skipping)
                continue
            if tag != qn("w:r"):
                continue
            skipping = self._run(child, out, skipping)
        return skipping

    def _run(self, run: etree._Element, out: list[Inline], skipping: bool) -> bool:
        field = run.find(qn("w:fldChar"))
        if field is not None:
            kind = field.get(qn("w:fldCharType"))
            if kind == "begin":
                self._instruction = []
                return True
            if kind == "separate":
                self._emit_field(out)
                return True          # skip the cached result
            if kind == "end":
                return False
        instruction = run.find(qn("w:instrText"))
        if instruction is not None:
            self._instruction.append(instruction.text or "")
            return True
        if skipping:
            return True
        if run.find(qn("w:footnoteReference")) is not None:
            number = run.find(qn("w:footnoteReference")).get(qn("w:id")) or ""
            out.append(FootnoteReference(label=number))
            return skipping

        properties = run.find(qn("w:rPr"))
        bold = properties is not None and properties.find(qn("w:b")) is not None
        italic = properties is not None and properties.find(qn("w:i")) is not None
        for child in run:
            if not isinstance(child.tag, str):
                continue
            if child.tag == qn("w:t"):
                node: Inline = Text(child.text or "")
            elif child.tag == qn("w:tab"):
                node = Text("\t")
            elif child.tag in (qn("w:br"), qn("w:cr")):
                node = LineBreak()
            elif child.tag == qn("w:drawing"):
                image = self._image(child)
                if image is None:
                    continue
                node = image
            else:
                continue
            if bold or italic:
                node = Emphasis(children=[node], strong=bold)
            out.append(node)
        return skipping

    _instruction: list[str]

    def _emit_field(self, out: list[Inline]) -> None:
        text = "".join(getattr(self, "_instruction", []))
        match = REF_FIELD.search(text)
        if match:
            out.append(CrossReference(label=self._label_for(match.group(1))))
            return
        sequence = SEQ_FIELD.search(text)
        if sequence:
            # The field's cached number is not carried -- it would freeze --
            # but the caption still has to read "Table 3.", so the number is
            # recounted here and regenerated as a field on the way back in.
            kind = sequence.group(1).title()
            self._sequence_counts[kind] = self._sequence_counts.get(kind, 0) + 1
            out.append(Text(str(self._sequence_counts[kind])))
            return
        if TOC_FIELD.search(text):
            return
        self.warnings.append(f"field dropped: {text.strip()[:60]}")

    def _label_for(self, bookmark: str) -> str:
        return self._label_of.get(bookmark, bookmark)

    def _href(self, rid: str) -> str:
        rel = self.pkg.rels(self.pkg.main_document).get(rid)
        return rel.target if rel else ""

    def _image(self, drawing: etree._Element) -> Image | None:
        blip = drawing.find(f".//{qn('a:blip')}")
        if blip is None:
            return None
        rid = blip.get(qn("r:embed"))
        rel = self.pkg.rels(self.pkg.main_document).get(rid) if rid else None
        target = rel.resolve(self.pkg.main_document) if rel else None
        part = self.pkg.actual_name(target) if target else None
        if part is None:
            return None
        name = part.rsplit("/", 1)[-1]
        if self.media_dir is not None:
            self.media_dir.mkdir(parents=True, exist_ok=True)
            (self.media_dir / name).write_bytes(self.pkg.blob(part))
            src = f"{self.media_dir.name}/{name}"
        else:
            src = part
        alt = ""
        doc_pr = drawing.find(f".//{qn('wp:docPr')}")
        if doc_pr is not None:
            alt = doc_pr.get("descr") or ""
        return Image(src=src, alt=alt)

    # -- tables ----------------------------------------------------------

    def _table(self, block: Block) -> Table:
        from ..oox.walk import _iter_cells, _iter_rows

        rows: list[TableRow] = []
        for index, row in enumerate(_iter_rows(block.element)):
            header = index == 0 or row.find(
                f"{qn('w:trPr')}/{qn('w:tblHeader')}"
            ) is not None
            cells = [
                TableCell(
                    blocks=[Paragraph(children=self._inlines(paragraph))
                            for paragraph in cell.findall(qn("w:p"))]
                    or [Paragraph()],
                    header=header,
                )
                for cell in _iter_cells(row)
            ]
            rows.append(TableRow(cells=cells, header=header))
        columns = max((len(r.cells) for r in rows), default=0)
        return Table(rows=rows, alignments=self._alignments(block.element, columns))

    def _alignments(self, table: etree._Element, columns: int) -> list[str]:
        """Read the column alignments back off the first body row.

        Markdown records alignment per column, Word per paragraph. The first
        body row is the best available sample -- the header is usually
        centred regardless of the column's alignment.
        """
        from ..oox.walk import _iter_cells, _iter_rows

        rows = list(_iter_rows(table))
        sample = rows[1] if len(rows) > 1 else (rows[0] if rows else None)
        out = ["left"] * columns
        if sample is None:
            return out
        for index, cell in enumerate(_iter_cells(sample)):
            if index >= columns:
                break
            jc = cell.find(f"{qn('w:p')}/{qn('w:pPr')}/{qn('w:jc')}")
            value = jc.get(qn("w:val")) if jc is not None else None
            if value in ("center", "right"):
                out[index] = value
        return out

    # -- the rest --------------------------------------------------------

    def footnotes(self) -> dict[str, Footnote]:
        part = self.pkg.related(RT["footnotes"])
        if not part or part not in self.pkg:
            return {}
        out: dict[str, Footnote] = {}
        for note in self.pkg.element(part).findall(qn("w:footnote")):
            if (note.get(qn("w:type")) or "normal") != "normal":
                continue
            label = note.get(qn("w:id")) or "?"
            blocks = [
                Paragraph(children=self._inlines(paragraph))
                for paragraph in note.findall(qn("w:p"))
            ]
            out[label] = Footnote(label=label, blocks=blocks)
        return out

    def meta(self) -> dict:
        """Front matter: the title, plus the document's own content controls."""
        out: dict = {}
        if self._title:
            out["title"] = self._title
        root = self.pkg.element(self.pkg.main_document)
        for sdt in root.iter(qn("w:sdt")):
            properties = sdt.find(qn("w:sdtPr"))
            content = sdt.find(qn("w:sdtContent"))
            if properties is None or content is None:
                continue
            tag = properties.find(qn("w:tag"))
            name = (tag.get(qn("w:val")) if tag is not None else "") or ""
            if not name.startswith("formgen."):
                continue
            if properties.find(qn("w:showingPlcHdr")) is not None:
                continue
            text = "".join(
                node.text or "" for node in content.iter(qn("w:t"))
            ).strip()
            if text:
                out[name[len("formgen."):]] = text
        return out


def _merge_text(inlines: list[Inline]) -> list[Inline]:
    """Join adjacent Text nodes -- Word splits runs for reasons of its own."""
    out: list[Inline] = []
    for inline in inlines:
        if isinstance(inline, Text) and out and isinstance(out[-1], Text):
            out[-1] = Text(out[-1].value + inline.value)
            continue
        if isinstance(inline, Emphasis) and out and isinstance(out[-1], Emphasis) \
                and out[-1].strong == inline.strong:
            out[-1].children = _merge_text(out[-1].children + inline.children)
            continue
        out.append(inline)
    return [i for i in out if not (isinstance(i, Text) and i.value == "")]


# -- serialization --------------------------------------------------------


def escape(text: str) -> str:
    return _ESCAPE.sub(r"\\\1", text)


def render_inlines(inlines: list[Inline]) -> str:
    out: list[str] = []
    for inline in inlines:
        if isinstance(inline, Text):
            out.append(escape(inline.value))
        elif isinstance(inline, Emphasis):
            marker = "**" if inline.strong else "*"
            inner = render_inlines(inline.children)
            out.append(f"{marker}{inner}{marker}" if inner.strip() else inner)
        elif isinstance(inline, Code):
            out.append(f"`{inline.value}`")
        elif isinstance(inline, Link):
            out.append(f"[{render_inlines(inline.children)}]({inline.href})")
        elif isinstance(inline, CrossReference):
            out.append(f"[](#{inline.label})")
        elif isinstance(inline, FootnoteReference):
            out.append(f"[^{inline.label}]")
        elif isinstance(inline, Math):
            out.append(f"${inline.tex}$")
        elif isinstance(inline, LineBreak):
            out.append("\n")
        elif isinstance(inline, Image):
            out.append(f"![{escape(inline.alt)}]({inline.src})")
    return "".join(out)


def to_markdown(doc: ContentDoc) -> str:
    parts: list[str] = []
    if doc.meta:
        parts.append(
            "---\n"
            + yaml.safe_dump(doc.meta, sort_keys=False, allow_unicode=True).rstrip()
            + "\n---"
        )
    for block in doc.blocks:
        rendered = _render_block(block)
        if rendered:
            parts.append(rendered)
    for label in sorted(doc.footnotes):
        text = " ".join(
            render_inlines(b.children) for b in doc.footnotes[label].blocks
            if hasattr(b, "children")
        ).strip()
        parts.append(f"[^{label}]: {text}")
    return "\n\n".join(parts).rstrip() + "\n"


def _render_block(block, indent: str = "") -> str:
    if isinstance(block, Heading):
        label = f" {{#{block.label}}}" if block.label else ""
        return f"{'#' * max(1, min(6, block.level))} " \
               f"{render_inlines(block.children)}{label}"
    if isinstance(block, Paragraph):
        label = f" {{#{block.label}}}" if block.label else ""
        return render_inlines(block.children) + label
    if isinstance(block, ListBlock):
        return _render_list(block, indent)
    if isinstance(block, Table):
        return _render_table(block)
    if isinstance(block, Figure):
        label = f"{{#{block.label}}}" if block.label else ""
        lines = [
            f"![{escape(block.image.alt)}]({block.image.src}){label}"
        ]
        if block.caption is not None:
            lines.append("")
            lines.append(render_inlines(block.caption.children))
        return "\n".join(lines)
    if isinstance(block, CodeBlock):
        return f"```{block.language}\n{block.value}\n```"
    if isinstance(block, MathBlock):
        return f"$$\n{block.tex}\n$$"
    if isinstance(block, TableOfContents):
        return "<!-- toc -->"
    if isinstance(block, PageBreak):
        return "<!-- pagebreak -->"
    from .ast import BlockQuote, Directive, ThematicBreak

    if isinstance(block, BlockQuote):
        inner = "\n".join(_render_block(b) for b in block.blocks)
        return "\n".join(f"> {line}" for line in inner.split("\n"))
    if isinstance(block, Directive):
        inner = "\n\n".join(_render_block(b) for b in block.blocks)
        return f"::: {block.name}\n{inner}\n:::"
    if isinstance(block, ThematicBreak):
        return "---"
    return ""


def _render_list(block: ListBlock, indent: str = "") -> str:
    lines: list[str] = []
    number = block.start
    for item in block.items:
        marker = f"{number}." if block.ordered else "-"
        number += 1
        first = True
        for inner in item.blocks:
            if isinstance(inner, ListBlock):
                lines.append(_render_list(inner, indent + "  "))
                continue
            text = _render_block(inner, indent)
            if first:
                lines.append(f"{indent}{marker} {text}")
                first = False
            else:
                lines.append(f"{indent}  {text}")
    return "\n".join(lines)


def _render_table(block: Table) -> str:
    rows = [[cell.text().strip() for cell in row.cells] for row in block.rows]
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    rows = [row + [""] * (columns - len(row)) for row in rows]
    widths = [
        max(3, *(len(row[index]) for row in rows)) for index in range(columns)
    ]
    alignments = (block.alignments + ["left"] * columns)[:columns]

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(
            cell.ljust(widths[i]) for i, cell in enumerate(cells)
        ) + " |"

    def rule() -> str:
        marks = []
        for index, alignment in enumerate(alignments):
            width = widths[index]
            if alignment == "right":
                marks.append("-" * (width - 1) + ":")
            elif alignment == "center":
                marks.append(":" + "-" * (width - 2) + ":")
            else:
                marks.append("-" * width)
        return "| " + " | ".join(marks) + " |"

    out = [line(rows[0]), rule()] + [line(row) for row in rows[1:]]
    caption = (f"{render_inlines(block.caption.children)}"
               + (f" {{#{block.label}}}" if block.label else "")
               if block.caption is not None else "")
    return (caption + "\n\n" if caption else "") + "\n".join(out)
