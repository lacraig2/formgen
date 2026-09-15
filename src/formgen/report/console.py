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


# -- rendering a plan -----------------------------------------------------

def render_plan(plan, console: "Console", verbose: bool = False) -> None:
    """Print a lint report a person can act on without opening a debugger.

    Findings are grouped by severity and each carries its locator, because a
    finding you cannot find is not a finding. `--verbose` adds the
    classification evidence, which is what `explain` shows for one paragraph.
    """
    from ..plan.model import ERROR, INFO, WARN

    console.write(f"{plan.document or '(document)'} -> {plan.profile or '(no profile)'}")
    stats = plan.stats or {}
    if stats:
        console.write(
            f"  {stats.get('blocks', 0)} blocks, "
            f"{stats.get('paragraphs', 0)} paragraphs"
            + (f"; body text is {stats['modal_body_size']}"
               if stats.get("modal_body_size") else "")
        )
    if plan.refusals:
        console.blank()
        console.write("REFUSED")
        for reason in plan.refusals:
            console.bullet(reason)
        return

    counts = plan.counts()
    console.write(
        f"  {counts[ERROR]} error(s), {counts[WARN]} warning(s), "
        f"{counts[INFO]} note(s)"
    )
    if not plan.findings:
        console.blank()
        console.write("  No deviations from the profile.")
        return

    for severity, title in ((ERROR, "ERRORS"), (WARN, "WARNINGS"), (INFO, "NOTES")):
        group = [f for f in plan.sorted_findings() if f.severity == severity]
        if not group:
            continue
        console.blank()
        console.write(f"{title} ({len(group)})")
        for finding in group:
            for line in finding.render():
                console.write(line)
            if verbose and finding.evidence:
                for item in finding.evidence:
                    console.write(f"     . {item}")

    review = plan.needs_review
    if review:
        console.blank()
        console.write(f"NEEDS REVIEW ({len(review)})")
        console.write(
            "  These classifications are guesses. `apply` would leave them "
            "alone unless you confirm them."
        )
        for edit in review[:10]:
            console.blank()
            console.write(f"  {edit.role} ({edit.confidence:.2f})")
            for line in edit.locator.render():
                console.write(line)
            for item in edit.evidence[:3]:
                console.write(f"     . {item}")
        if len(review) > 10:
            console.write(f"  ... {len(review) - 10} more")
