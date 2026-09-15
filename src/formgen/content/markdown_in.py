"""Markdown -> ContentDoc.

YAML front matter holds the placeholders; the body is CommonMark plus the four
extensions in `plugins.py`. Nothing here knows what anything looks like -- the
profile decides that at emit time -- so this module's only job is to read
meaning out of the source faithfully and to complain early about anything it
cannot.

Complaining early is the point of `validate`: pre-flight runs before a single
byte is written, and a missing placeholder lists **every** missing key at once
with the `--set` line to paste. Discovering them one run at a time is the
difference between a tool people use and a tool people script around.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.token import Token

from .ast import (
    BlockQuote, Code, CodeBlock, ContentDoc, CrossReference, Directive,
    Emphasis, Figure, Footnote, FootnoteReference, Heading, Image, Inline,
    LineBreak, Link, ListBlock, ListItem, Math, MathBlock, PageBreak,
    Paragraph, Table, TableCell, TableOfContents, TableRow, Text,
    ThematicBreak,
)
from .plugins import formgen_plugins

FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.S)
TOC_COMMENT = re.compile(r"<!--\s*toc\s*-->", re.I)
PAGE_BREAK_COMMENT = re.compile(r"<!--\s*(page-?break|newpage)\s*-->", re.I)
UNRESOLVED = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def parser() -> MarkdownIt:
    """CommonMark plus pipe tables plus our four extensions."""
    return MarkdownIt("commonmark").enable("table").use(formgen_plugins)


def split_front_matter(source: str) -> tuple[dict[str, Any], str, int]:
    """(metadata, body, lines consumed). Never raises on a missing block."""
    match = FRONT_MATTER.match(source)
    if not match:
        return {}, source, 0
    try:
        # safe_load only: front matter is user data, and a document should
        # never be able to construct a Python object.
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"the YAML front matter is malformed: {exc}") from exc
    if not isinstance(meta, dict):
        raise ValueError("the YAML front matter must be a mapping of keys to values")
    return meta, source[match.end():], match.group(0).count("\n")


def parse(source: str) -> ContentDoc:
    """Read a Markdown document into the AST."""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    meta, body, _ = split_front_matter(source)
    tokens = parser().parse(body)
    doc = ContentDoc(meta=meta)
    builder = _Builder(doc)
    doc.blocks = builder.blocks(tokens)
    _pair_captions(doc)
    return doc


class _Builder:
    def __init__(self, doc: ContentDoc):
        self.doc = doc

    def blocks(self, tokens: list[Token]) -> list:
        out: list = []
        index = 0
        while index < len(tokens):
            index = self._block(tokens, index, out)
        return out

    def _block(self, tokens: list[Token], index: int, out: list) -> int:
        token = tokens[index]
        kind = token.type

        if kind == "heading_open":
            inline = tokens[index + 1]
            out.append(Heading(
                level=int(token.tag[1:]),
                children=self.inlines(inline.children or []),
                label=(token.meta or {}).get("label", ""),
            ))
            return index + 3
        if kind == "paragraph_open":
            inline = tokens[index + 1]
            children = self.inlines(inline.children or [])
            label = (token.meta or {}).get("label", "")
            # A paragraph that is only an image is a figure: the caption is
            # attached afterwards, once we know what follows it.
            if len(children) == 1 and isinstance(children[0], Image):
                image = children[0]
                out.append(Figure(image=image, label=image.label or label))
            else:
                out.append(Paragraph(children=children, label=label))
            return index + 3
        if kind == "fence" or kind == "code_block":
            out.append(CodeBlock(value=token.content.rstrip("\n"),
                                 language=(token.info or "").strip()))
            return index + 1
        if kind == "math_block":
            out.append(MathBlock(tex=token.content,
                                 label=(token.meta or {}).get("label", "")))
            return index + 1
        if kind == "bullet_list_open" or kind == "ordered_list_open":
            return self._list(tokens, index, out)
        if kind == "blockquote_open":
            inner, index = self._until(tokens, index + 1, "blockquote_close")
            out.append(BlockQuote(blocks=self.blocks(inner)))
            return index
        if kind == "directive_open":
            inner, index = self._until(tokens, index + 1, "directive_close")
            out.append(Directive(name=(token.meta or {}).get("name", ""),
                                 blocks=self.blocks(inner)))
            return index
        if kind == "table_open":
            inner, index = self._until(tokens, index + 1, "table_close")
            out.append(self._table(inner, token))
            return index
        if kind == "footnote_def":
            meta = token.meta or {}
            label = meta.get("label", "")
            self.doc.footnotes[label] = Footnote(
                label=label,
                blocks=self.blocks(parser().parse(meta.get("content", ""))),
            )
            return index + 1
        if kind == "hr":
            out.append(ThematicBreak())
            return index + 1
        if kind == "html_block":
            if TOC_COMMENT.search(token.content):
                out.append(TableOfContents())
            elif PAGE_BREAK_COMMENT.search(token.content):
                out.append(PageBreak())
            else:
                self.doc.warnings.append(
                    f"raw HTML has no .docx equivalent and was dropped: "
                    f"{token.content.strip()[:60]}"
                )
            return index + 1
        return index + 1

    def _until(self, tokens: list[Token], index: int, closing: str) -> tuple[list, int]:
        depth = 1
        opening = closing.replace("_close", "_open")
        inner: list[Token] = []
        while index < len(tokens):
            token = tokens[index]
            if token.type == opening:
                depth += 1
            elif token.type == closing:
                depth -= 1
                if depth == 0:
                    return inner, index + 1
            inner.append(token)
            index += 1
        return inner, index

    def _list(self, tokens: list[Token], index: int, out: list) -> int:
        token = tokens[index]
        ordered = token.type == "ordered_list_open"
        start = int((token.attrGet("start") or 1)) if ordered else 1
        closing = "ordered_list_close" if ordered else "bullet_list_close"
        inner, index = self._until(tokens, index + 1, closing)

        items: list[ListItem] = []
        position = 0
        while position < len(inner):
            if inner[position].type != "list_item_open":
                position += 1
                continue
            body, position = self._until(inner, position + 1, "list_item_close")
            items.append(ListItem(blocks=self.blocks(body)))
        out.append(ListBlock(items=items, ordered=ordered, start=start))
        return index

    def _table(self, inner: list[Token], opening: Token) -> Table:
        rows: list[TableRow] = []
        alignments: list[str] = []
        header = False
        position = 0
        while position < len(inner):
            token = inner[position]
            if token.type == "thead_open":
                header = True
            elif token.type == "thead_close":
                header = False
            elif token.type == "tr_open":
                cells: list[TableCell] = []
                position += 1
                while position < len(inner) and inner[position].type != "tr_close":
                    cell = inner[position]
                    if cell.type in ("th_open", "td_open"):
                        style = cell.attrGet("style") or ""
                        if header:
                            alignments.append(_alignment(style))
                        content = inner[position + 1]
                        cells.append(TableCell(
                            blocks=[Paragraph(
                                children=self.inlines(content.children or [])
                            )],
                            header=cell.type == "th_open",
                        ))
                        position += 3
                        continue
                    position += 1
                rows.append(TableRow(cells=cells, header=header))
            position += 1
        return Table(rows=rows, alignments=alignments,
                     label=(opening.meta or {}).get("label", ""))

    # -- inlines ---------------------------------------------------------

    def inlines(self, tokens: list[Token]) -> list[Inline]:
        out: list[Inline] = []
        stack: list[tuple[str, list[Inline]]] = []
        current = out
        for token in tokens:
            kind = token.type
            if kind == "text":
                if token.content:
                    current.append(Text(token.content))
            elif kind == "code_inline":
                current.append(Code(token.content))
            elif kind in ("em_open", "strong_open"):
                node = Emphasis(children=[], strong=kind == "strong_open")
                current.append(node)
                stack.append((kind, current))
                current = node.children
            elif kind in ("em_close", "strong_close"):
                if stack:
                    _, current = stack.pop()
            elif kind == "link_open":
                href = token.attrGet("href") or ""
                node = Link(href=href, children=[])
                current.append(node)
                stack.append((kind, current))
                current = node.children
            elif kind == "link_close":
                if stack:
                    _, current = stack.pop()
                _demote_empty_link(current)
            elif kind == "image":
                current.append(_image(token))
            elif kind == "math_inline":
                current.append(Math(tex=token.content))
            elif kind == "footnote_ref":
                current.append(FootnoteReference((token.meta or {}).get("label", "")))
            elif kind in ("softbreak", "hardbreak"):
                current.append(LineBreak() if kind == "hardbreak" else Text(" "))
            elif kind == "html_inline":
                self.doc.warnings.append(
                    f"inline HTML has no .docx equivalent and was dropped: "
                    f"{token.content[:40]}"
                )
        return out


def _demote_empty_link(container: list[Inline]) -> None:
    """`[](#fig:panel)` is a cross-reference, not a link with no text.

    Word renders the two completely differently -- a REF field that renumbers
    itself versus a hyperlink to nowhere -- so the distinction is made here,
    once, rather than guessed at in the emitter.
    """
    if not container:
        return
    last = container[-1]
    if not isinstance(last, Link) or last.children:
        return
    if last.href.startswith("#"):
        container[-1] = CrossReference(label=last.href[1:])


def _image(token: Token) -> Image:
    label = ""
    src = token.attrGet("src") or ""
    alt = "".join(
        child.content for child in (token.children or []) if child.type == "text"
    )
    title = token.attrGet("title") or ""
    return Image(src=src, alt=alt, label=label, width=title)


_ALIGNMENTS = {"text-align:left": "left", "text-align:right": "right",
               "text-align:center": "center"}


def _alignment(style: str) -> str:
    return _ALIGNMENTS.get(style.replace(" ", ""), "left")


def _pair_captions(doc: ContentDoc) -> None:
    """Attach the paragraph after a figure, or before a table, as its caption.

    Word's own convention, and the one every style guide uses: figure
    captions go below, table captions above. Matching it means an author
    writes what they would have written anyway.
    """
    blocks = doc.blocks
    keep: list[bool] = [True] * len(blocks)
    for index, block in enumerate(blocks):
        if isinstance(block, Figure) and block.caption is None:
            following = index + 1
            if following < len(blocks) and _is_caption(blocks[following]):
                block.caption = blocks[following]
                keep[following] = False
        elif isinstance(block, Table) and block.caption is None:
            preceding = index - 1
            if preceding >= 0 and keep[preceding] and _is_caption(blocks[preceding]):
                block.caption = blocks[preceding]
                keep[preceding] = False
        # A label written on the caption belongs to the thing captioned.
        # Both spellings occur in the wild -- on the image, or on the caption
        # line under it -- and a cross-reference must resolve either way.
        caption = getattr(block, "caption", None)
        if caption is not None and not block.label and caption.label:
            block.label = caption.label
            caption.label = ""
    doc.blocks = [b for b, alive in zip(blocks, keep) if alive]


_CAPTION_LEAD = re.compile(r"^\s*(figure|fig\.?|table)\s*[\d{]", re.I)


def _is_caption(block) -> bool:
    return isinstance(block, Paragraph) and bool(_CAPTION_LEAD.match(block.text()))


# -- pre-flight -----------------------------------------------------------


@dataclass
class Problem:
    code: str
    message: str
    remedy: str = ""


def validate(
    doc: ContentDoc,
    required: set[str] | None = None,
    known_roles: set[str] | None = None,
) -> list[Problem]:
    """Everything wrong with the source, reported at once and before any write."""
    problems: list[Problem] = []
    required = required or set()
    supplied = {k for k, v in doc.meta.items() if v not in (None, "")}

    missing = sorted(required - supplied)
    if missing:
        setters = " ".join(f'--set {name}="..."' for name in missing)
        problems.append(Problem(
            "placeholder.missing",
            f"{len(missing)} required field(s) have no value: "
            + ", ".join(missing),
            f"supply them in the front matter, or: {setters}",
        ))

    labels = doc.labels()
    for reference in sorted(set(doc.references())):
        if reference not in labels:
            problems.append(Problem(
                "reference.dangling",
                f"[](#{reference}) points at a label nothing defines",
                "add {#" + reference + "} to the figure, table or heading it "
                "should point at.",
            ))

    defined = set(doc.footnotes)
    for label in sorted(set(doc.footnote_labels_used())):
        if label not in defined:
            problems.append(Problem(
                "footnote.undefined",
                f"[^{label}] has no definition",
                f"add a line reading [^{label}]: ...",
            ))
    for label in sorted(defined - set(doc.footnote_labels_used())):
        problems.append(Problem(
            "footnote.unused",
            f"[^{label}]: is defined but never referenced",
            "Word will not render it; remove it or cite it.",
        ))

    if known_roles is not None:
        for block in doc.walk():
            if isinstance(block, Directive) and block.name not in known_roles:
                problems.append(Problem(
                    "directive.unknown",
                    f"::: {block.name} is not a role this profile defines",
                    "known roles: " + ", ".join(sorted(known_roles)),
                ))

    for block in doc.walk():
        text = getattr(block, "text", lambda: "")()
        for match in UNRESOLVED.finditer(text or ""):
            problems.append(Problem(
                "placeholder.unresolved",
                f"{{{{{match.group(1)}}}}} was never substituted",
                "a template placeholder reached the output verbatim.",
            ))
    return problems
