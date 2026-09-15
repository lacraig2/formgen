"""Console output that survives a Windows code page.

The #1 crash risk on the target machine is not a logic bug, it is
``UnicodeEncodeError`` on a cp1252 console. Report snippets come straight out
of user documents -- em dashes, curly quotes, non-breaking hyphens, bullets --
and Python's default stdout encoding on Windows is the console code page, not
UTF-8. So all default output is ASCII-safe, and ``--unicode`` opts back in for
a terminal that can take it.

This is the classic "worked on my Linux laptop" failure, and it is cheap to
prevent and expensive to discover in front of a user.
"""

from __future__ import annotations

import sys
from typing import Any, TextIO

# Characters we emit or quote often enough to be worth a real transliteration
# rather than a "?" -- the output stays readable rather than merely surviving.
_FOLD = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "--", "−": "-", "‑": "-",
    "…": "...", " ": " ", " ": " ", " ": " ",
    "•": "*", "▪": "*", "■": "*", "◆": "*", "➢": ">",
    "→": "->", "←": "<-", "✓": "v", "✔": "v",
    "×": "x", "·": ".", "­": "", "￼": "[object]",
    "─": "-", "│": "|", "┌": "+", "┐": "+",
    "└": "+", "┘": "+", "├": "+", "┤": "+",
}

_TABLE = str.maketrans(_FOLD)


def asciify(text: str) -> str:
    """Fold to ASCII, transliterating what we can and dropping what we cannot."""
    folded = text.translate(_TABLE)
    return folded.encode("ascii", "replace").decode("ascii")


class Console:
    """Writes to a stream, folding to ASCII unless told otherwise."""

    def __init__(self, stream: TextIO | None = None, unicode: bool = False):
        self.stream = stream if stream is not None else sys.stdout
        self.unicode = unicode

    def write(self, text: str = "") -> None:
        if not self.unicode:
            text = asciify(text)
        try:
            print(text, file=self.stream)
        except UnicodeEncodeError:
            # The stream lied about what it could take. Never let output
            # formatting be the thing that kills a run.
            print(asciify(text), file=self.stream)

    def blank(self) -> None:
        self.write("")

    def bullet(self, text: str, indent: int = 2) -> None:
        self.write(" " * indent + "- " + text)

    def rule(self, title: str = "", width: int = 64) -> None:
        if not title:
            self.write("-" * width)
            return
        self.write(f"-- {title} " + "-" * max(0, width - len(title) - 4))

    def table(self, rows: list[tuple[Any, ...]], headers: tuple[str, ...]) -> None:
        cells = [tuple(str(c) for c in row) for row in rows]
        widths = [len(h) for h in headers]
        for row in cells:
            for i, cell in enumerate(row[: len(widths)]):
                widths[i] = max(widths[i], len(cell))
        self.write("  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip())
        self.write("  ".join("-" * w for w in widths))
        for row in cells:
            self.write("  ".join(
                c.ljust(w) for c, w in zip(row, widths)
            ).rstrip())
