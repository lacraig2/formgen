"""The content model `new` writes into and `extract` reads out of.

One AST, two directions. `emit` turns it into OOXML and `extract` turns OOXML
back into it, which makes ``extract(emit(md)) ~= md`` a genuine test oracle
rather than an aspiration -- and the reason the two are built as a pair.

The model is deliberately about *meaning*, not appearance: a Heading knows its
level, not its font; a Figure knows it has a caption, not that the caption is
9pt italic. Appearance comes from the profile at emit time. That separation is
what lets one Markdown file render into two different house formats, and it is
why `extract` can produce Markdown that survives being re-emitted.

A note on what is deliberately absent: there is no generic "styled span" and
no arbitrary attribute bag. Anything that cannot be expressed here cannot
round-trip, and silently dropping it on the way back would be worse than
refusing it on the way in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

# -- inlines --------------------------------------------------------------


@dataclass
class Inline:
    """Base for everything inside a paragraph."""

    def text(self) -> str:
        return ""


@dataclass
class Text(Inline):
    value: str = ""

    def text(self) -> str:
        return self.value


@dataclass
class Emphasis(Inline):
    children: list[Inline] = field(default_factory=list)
    strong: bool = False

    def text(self) -> str:
        return "".join(c.text() for c in self.children)


@dataclass
class Code(Inline):
    value: str = ""

    def text(self) -> str:
        return self.value


@dataclass
class Link(Inline):
    href: str = ""
    children: list[Inline] = field(default_factory=list)

    def text(self) -> str:
        return "".join(c.text() for c in self.children) or self.href


@dataclass
class CrossReference(Inline):
    """`[](#fig:panel)` -- a REF field, not a hyperlink.

    Kept distinct from Link because the two produce completely different
    OOXML: a link is a relationship to a URL, a cross-reference is a field
    Word recomputes when the numbering changes.
    """

    label: str = ""
    kind: str = ""            # "figure" | "table" | "heading" | ""

    def text(self) -> str:
        return self.label


@dataclass
class FootnoteReference(Inline):
    label: str = ""

    def text(self) -> str:
        return ""


@dataclass
class Math(Inline):
    tex: str = ""

    def text(self) -> str:
        return self.tex


@dataclass
class LineBreak(Inline):
    def text(self) -> str:
        return "\n"


@dataclass
class Image(Inline):
    src: str = ""
    alt: str = ""
    label: str = ""
    width: str = ""

    def text(self) -> str:
        return self.alt


# -- blocks ---------------------------------------------------------------


@dataclass
class Block:
    def walk(self) -> Iterator[Block]:
        yield self


@dataclass
class Paragraph(Block):
    children: list[Inline] = field(default_factory=list)
    role: str = "body"
    label: str = ""

    def text(self) -> str:
        return "".join(c.text() for c in self.children)


@dataclass
class Heading(Block):
    level: int = 1
    children: list[Inline] = field(default_factory=list)
    label: str = ""

    def text(self) -> str:
        return "".join(c.text() for c in self.children)


@dataclass
class ListItem(Block):
    blocks: list[Block] = field(default_factory=list)

    def walk(self) -> Iterator[Block]:
        yield self
        for block in self.blocks:
            yield from block.walk()


@dataclass
class ListBlock(Block):
    items: list[ListItem] = field(default_factory=list)
    ordered: bool = False
    start: int = 1
    level: int = 0

    def walk(self) -> Iterator[Block]:
        yield self
        for item in self.items:
            yield from item.walk()


@dataclass
class TableCell(Block):
    blocks: list[Block] = field(default_factory=list)
    header: bool = False

    def text(self) -> str:
        return " ".join(
            b.text() for b in self.blocks if hasattr(b, "text")
        )


@dataclass
class TableRow(Block):
    cells: list[TableCell] = field(default_factory=list)
    header: bool = False


@dataclass
class Table(Block):
    rows: list[TableRow] = field(default_factory=list)
    alignments: list[str] = field(default_factory=list)
    caption: Paragraph | None = None
    label: str = ""

    def walk(self) -> Iterator[Block]:
        yield self
        for row in self.rows:
            for cell in row.cells:
                for block in cell.blocks:
                    yield from block.walk()


@dataclass
class Figure(Block):
    image: Image = field(default_factory=Image)
    caption: Paragraph | None = None
    label: str = ""


@dataclass
class CodeBlock(Block):
    value: str = ""
    language: str = ""

    def text(self) -> str:
        return self.value


@dataclass
class BlockQuote(Block):
    blocks: list[Block] = field(default_factory=list)

    def walk(self) -> Iterator[Block]:
        yield self
        for block in self.blocks:
            yield from block.walk()


@dataclass
class MathBlock(Block):
    tex: str = ""
    label: str = ""

    def text(self) -> str:
        return self.tex


@dataclass
class Directive(Block):
    """`::: distribution-statement` -- a house oddity with no Markdown form.

    The escape hatch that keeps the dialect small: rather than inventing
    syntax for every institution's boilerplate block, a directive names a
    role and the profile decides what that role looks like.
    """

    name: str = ""
    blocks: list[Block] = field(default_factory=list)

    def walk(self) -> Iterator[Block]:
        yield self
        for block in self.blocks:
            yield from block.walk()


@dataclass
class TableOfContents(Block):
    levels: tuple[int, int] = (1, 3)


@dataclass
class PageBreak(Block):
    pass


@dataclass
class ThematicBreak(Block):
    pass


@dataclass
class Footnote(Block):
    label: str = ""
    blocks: list[Block] = field(default_factory=list)

    def walk(self) -> Iterator[Block]:
        yield self
        for block in self.blocks:
            yield from block.walk()


@dataclass
class ContentDoc:
    """A whole document: the front matter, the body, and the footnotes."""

    meta: dict[str, Any] = field(default_factory=dict)
    blocks: list[Block] = field(default_factory=list)
    footnotes: dict[str, Footnote] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def walk(self) -> Iterator[Block]:
        for block in self.blocks:
            yield from block.walk()

    def labels(self) -> dict[str, str]:
        """label -> kind, for resolving cross-references.

        Built from the document rather than declared, so a reference to a
        label that does not exist is detectable before anything is written.
        """
        out: dict[str, str] = {}
        for block in self.walk():
            label = getattr(block, "label", "")
            if not label:
                continue
            out[label] = (
                "figure" if isinstance(block, Figure)
                else "table" if isinstance(block, Table)
                else "heading" if isinstance(block, Heading)
                else "equation" if isinstance(block, MathBlock)
                else "paragraph"
            )
        return out

    def references(self) -> list[str]:
        out: list[str] = []
        for block in self.walk():
            for inline in _inlines_of(block):
                if isinstance(inline, CrossReference):
                    out.append(inline.label)
        return out

    def footnote_labels_used(self) -> list[str]:
        out: list[str] = []
        for block in self.walk():
            for inline in _inlines_of(block):
                if isinstance(inline, FootnoteReference):
                    out.append(inline.label)
        return out


def _inlines_of(block: Block) -> Iterator[Inline]:
    children = getattr(block, "children", None)
    if children:
        yield from _flatten(children)
    caption = getattr(block, "caption", None)
    if caption is not None:
        yield from _flatten(caption.children)


def _flatten(inlines: list[Inline]) -> Iterator[Inline]:
    for inline in inlines:
        yield inline
        nested = getattr(inline, "children", None)
        if nested:
            yield from _flatten(nested)


def plain_text(inlines: list[Inline]) -> str:
    return "".join(i.text() for i in inlines)
