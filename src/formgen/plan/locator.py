"""Telling a human where in their document the problem is.

A block index is worthless to someone looking at Word. So every finding
carries four ways to be found, in increasing order of how much we had to work
for them:

* **heading_path** -- computed from the *classified* heading stream, not from
  w:pStyle, so it works on the Google-Docs export where every paragraph is
  Normal and nothing is styled as a heading at all;
* **container** -- "table 3, row 2", "footnote 7", "header", so a finding
  inside a cell does not read as if it were in the body;
* **find_string** -- five to eight words the user pastes into Word's Find box,
  extended until it is unique in the document. This is the one people
  actually use;
* **page** -- filled in only when Word was available to paginate. Never
  guessed: a wrong page number is worse than no page number, because it costs
  the reader the time to go and look.

The find string is built from collapsed *raw* text rather than the normalised
form, because it has to survive being pasted into a search box. Normalisation
is for comparison; this is for humans.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from ..oox.walk import Block, match_key

MIN_WORDS = 5
MAX_WORDS = 14
_WS = re.compile(r"\s+")


def searchable(text: str) -> str:
    """Collapse whitespace but keep the characters Word will match on."""
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class Locator:
    part: str = ""
    path: str = ""
    heading_path: tuple[str, ...] = ()
    ordinal: int = 0
    of: int = 0
    container: str = "body"
    find_string: str = ""
    para_id: str | None = None
    page: int | None = None

    def describe(self) -> str:
        """One line naming the place, in the order a person would look."""
        if self.container == "document":
            return "the document as a whole"
        where = " > ".join(self.heading_path) if self.heading_path else "(no heading)"
        bits = [where]
        if self.ordinal and self.of:
            bits.append(f"paragraph {self.ordinal} of {self.of}")
        elif self.ordinal:
            bits.append(f"paragraph {self.ordinal}")
        if self.container and self.container != "body":
            bits.append(self.container)
        if self.page is not None:
            bits.append(f"page {self.page}")
        return "  ".join(bits)

    def render(self, indent: str = "     ") -> list[str]:
        lines = [f"{indent}{self.describe()}"]
        if self.find_string:
            lines.append(f'{indent}Find: "{self.find_string}"')
        return lines


class FindStrings:
    """Builds search strings that are unique within one document.

    Uniqueness is checked against *substring* occurrence, not equality:
    "The panel was soaked" is useless as a find string if it is the opening of
    four paragraphs, even though no two of those paragraphs are identical.
    """

    def __init__(self, texts: Iterable[str]):
        self._corpus = [searchable(t) for t in texts if t and t.strip()]
        self._joined = "\n".join(self._corpus)

    def occurrences(self, needle: str) -> int:
        if not needle:
            return 0
        return self._joined.count(needle)

    def for_text(self, text: str) -> str:
        words = searchable(text).split()
        if not words:
            return ""
        for count in range(MIN_WORDS, MAX_WORDS + 1):
            candidate = " ".join(words[:count])
            if self.occurrences(candidate) <= 1:
                return candidate
            if count >= len(words):
                break
        # Genuinely repeated boilerplate: the longest prefix we have is still
        # the most useful thing to hand over, and the ordinal in the locator
        # disambiguates it.
        return " ".join(words[:MAX_WORDS])


@dataclass
class LocatorFactory:
    """Turns a classified block stream into locators.

    Constructed once per document, because both the heading stack and the
    find-string uniqueness test need to see every block first.
    """

    blocks: Sequence[Block]
    roles: dict[str, str] = field(default_factory=dict)
    pages: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._find = FindStrings(b.text for b in self.blocks if b.is_paragraph)
        self._headings: dict[str, tuple[str, ...]] = {}
        self._ordinals: dict[str, tuple[int, int]] = {}
        self._table_index: dict[str, int] = {}
        self._compute()

    def _compute(self) -> None:
        stack: list[tuple[int, str]] = []
        section_blocks: list[str] = []
        tables = 0
        for block in self.blocks:
            if block.kind == "tbl":
                tables += 1
                self._table_index[block.path] = tables
            if not block.is_paragraph:
                continue
            role = self.roles.get(block.path, "")
            level = _heading_level(role)
            if level is not None:
                # A new heading closes every deeper one, then this section's
                # paragraph ordinals restart.
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, searchable(block.text) or "(untitled)"))
                self._flush(section_blocks)
                section_blocks = []
            self._headings[block.path] = tuple(title for _, title in stack)
            section_blocks.append(block.path)
        self._flush(section_blocks)

    def _flush(self, paths: list[str]) -> None:
        total = len(paths)
        for i, path in enumerate(paths, 1):
            self._ordinals[path] = (i, total)

    def container_of(self, block: Block) -> str:
        ctx = block.context
        if ctx.kind == "table":
            table = self._enclosing_table(block.path)
            where = f"row {ctx.row}, column {ctx.col}"
            prefix = f"table {table}, " if table else "table "
            if ctx.table_depth > 1:
                return f"{prefix}{where} (nested {ctx.table_depth} deep)"
            return f"{prefix}{where}"
        return ctx.describe()

    def _enclosing_table(self, path: str) -> int | None:
        best: int | None = None
        for tbl_path, index in self._table_index.items():
            if path.startswith(tbl_path + "/"):
                best = index
        return best

    def of(self, block: Block) -> Locator:
        ordinal, total = self._ordinals.get(block.path, (0, 0))
        return Locator(
            part=block.context.part,
            path=block.path,
            heading_path=self._headings.get(block.path, ()),
            ordinal=ordinal,
            of=total,
            container=self.container_of(block),
            find_string=self._find.for_text(block.text) if block.is_paragraph else "",
            para_id=block.para_id,
            page=self.pages.get(block.path),
        )


def _heading_level(role: str) -> int | None:
    if role == "title":
        return 0
    if role.startswith("heading"):
        try:
            return int(role[len("heading"):])
        except ValueError:
            return None
    return None


def document_locator(part: str = "") -> Locator:
    """For findings about the document as a whole rather than one block."""
    return Locator(part=part, container="document")


def same_place(a: Locator, b: Locator) -> bool:
    return (a.part, a.path) == (b.part, b.path)


def normalize_for_match(text: str) -> str:
    return match_key(text)
