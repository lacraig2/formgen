"""`formgen doctor` -- let Word grade our homework.

Three places in this codebase are most likely to be subtly wrong: the
`docDefaults` inheritance root, the `basedOn` chain, and theme indirection.
All three are invisible in the XML and obvious on screen. Word is installed on
the target machine, so the highest-value thing we can do with it is ask it to
resolve the same document we just resolved and diff the two answers.

The comparison is deliberately **paragraph-by-paragraph rather than
style-by-style**. Matching style definitions means matching style *names*, and
`w:styleId` is localised for built-ins while Word's `NameLocal` is localised
too -- so a mismatch could mean our resolver is wrong or could mean the user
runs a German Word, and we would not be able to tell which. Asking "what does
Word render this paragraph as?" needs no names at all, and it exercises the
whole cascade -- defaults, chain, theme, toggles, direct formatting -- in one
measurement.

Paragraph streams are aligned on their text before any formatting is compared.
If the streams diverge we say where and stop, rather than reporting a hundred
spurious differences caused by an off-by-one.

Everything here is opt-in and skipped with a notice when Word is unavailable,
so Linux CI is unaffected and no command depends on it.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..oox.props import ParaProps, RunProps
from ..oox.styles import StyleGraph
from ..oox.theme import Theme
from ..oox.walk import Walker, match_key
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from .com import (
    WD_DO_NOT_SAVE_CHANGES, WD_FORMAT_PDF, WordTimeout, WordUnavailable,
    available, open_document, run_guarded,
)

# Word returns this from any property that is not uniform over the range it
# was asked about. It means "the question does not have one answer", which our
# single-value model cannot express -- so it is skipped, not reported.
WD_UNDEFINED = 9999999

POINT_TOLERANCE = 0.06        # half a twip, plus float slop
ALIGNMENT = {0: "left", 1: "center", 2: "right", 3: "both", 4: "distribute"}
# wdLineSpacingRule
LINE_SINGLE, LINE_1_5, LINE_DOUBLE, LINE_AT_LEAST, LINE_EXACT, LINE_MULTIPLE = range(6)


@dataclass
class Difference:
    where: str
    property: str
    ours: Any
    words: Any

    def describe(self) -> str:
        return f"{self.where}: {self.property}  we say {self.ours}, Word says {self.words}"


@dataclass
class Check:
    name: str
    ok: bool | None            # None == skipped
    detail: str = ""
    differences: list[Difference] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "skip" if self.ok is None else ("ok" if self.ok else "FAIL")


@dataclass
class DoctorReport:
    document: Path
    checks: list[Check] = field(default_factory=list)
    skipped_reason: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.skipped_reason)

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks)

    def render(self) -> str:
        lines = [f"doctor: {self.document.name}"]
        if self.skipped:
            lines.append(f"  SKIPPED -- {self.skipped_reason}")
            lines.append(
                "  Everything formgen does works without Word; this check only "
                "adds confidence it cannot get on its own."
            )
            return "\n".join(lines)
        for check in self.checks:
            lines.append(f"  [{check.status:>4}] {check.name}  {check.detail}")
            for diff in check.differences[:12]:
                lines.append(f"           {diff.describe()}")
            if len(check.differences) > 12:
                lines.append(f"           ... {len(check.differences) - 12} more")
        return "\n".join(lines)


# -- reading what Word thinks --------------------------------------------


@dataclass
class WordParagraph:
    """One paragraph as Word resolved it. Kept as plain data so the
    comparison logic is testable without Word."""

    text: str
    font_name: Any = None
    font_size: Any = None
    bold: Any = None
    italic: Any = None
    alignment: Any = None
    space_before: Any = None
    space_after: Any = None
    left_indent: Any = None
    first_line_indent: Any = None
    line_spacing_rule: Any = None
    line_spacing: Any = None
    outline_level: Any = None


def read_paragraphs(doc: Any, limit: int = 400) -> list[WordParagraph]:
    """Pull the resolved formatting of the first `limit` body paragraphs.

    Each property is a separate COM round trip, so this is the slow part of
    doctor by a wide margin -- hence the cap. A few hundred paragraphs is far
    more than enough to catch a systematic resolver bug.
    """
    out: list[WordParagraph] = []
    count = min(int(doc.Paragraphs.Count), limit)
    for i in range(1, count + 1):
        paragraph = doc.Paragraphs(i)
        rng, fmt = paragraph.Range, paragraph.Format
        font = rng.Font
        out.append(WordParagraph(
            # Word terminates every paragraph with \r, and a cell paragraph
            # with \r\a. Neither is part of the text.
            text=str(rng.Text).replace("\x07", "").rstrip("\r"),
            font_name=font.Name,
            font_size=font.Size,
            bold=font.Bold,
            italic=font.Italic,
            alignment=fmt.Alignment,
            space_before=fmt.SpaceBefore,
            space_after=fmt.SpaceAfter,
            left_indent=fmt.LeftIndent,
            first_line_indent=fmt.FirstLineIndent,
            line_spacing_rule=fmt.LineSpacingRule,
            line_spacing=fmt.LineSpacing,
            outline_level=fmt.OutlineLevel,
        ))
    return out


# -- our own answer -------------------------------------------------------


@dataclass
class OurParagraph:
    text: str
    run: RunProps
    para: ParaProps


def read_our_paragraphs(pkg: OpcPackage, limit: int = 400) -> list[OurParagraph]:
    styles_part = pkg.related(RT["styles"])
    theme_part = pkg.related(RT["theme"])
    styles = StyleGraph.parse(
        pkg.element(styles_part),
        Theme.parse(pkg.element(theme_part) if theme_part and theme_part in pkg else None),
    ) if styles_part and styles_part in pkg else StyleGraph({}, RunProps(), ParaProps())

    out: list[OurParagraph] = []
    for block in Walker(pkg).blocks(include_aux=False):
        if not block.is_paragraph or block.context.in_del:
            continue
        if block.context.in_textbox:
            # Word puts text boxes in a separate story, so they are not in
            # doc.Paragraphs and including them would break the alignment.
            continue
        style_id = styles.para_style_or_default(block.style_id)
        direct_para = ParaProps.parse(block.element.find(qn("w:pPr")))
        first_run = next(
            (r for r in block.element.iter(qn("w:r"))
             if r.getparent() is not None and r.getparent().tag != qn("w:pPr")),
            None,
        )
        direct_run = RunProps.parse(
            first_run.find(qn("w:rPr")) if first_run is not None else None
        )
        out.append(OurParagraph(
            text=block.text,
            run=styles.effective_for_run(style_id, direct_run.style_id, direct_run),
            para=styles.effective_for_para(style_id, direct_para),
        ))
        if len(out) >= limit:
            break
    return out


# -- the comparison (pure, and therefore testable without Word) ----------


def _close(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return True
    return abs(float(a) - float(b)) <= POINT_TOLERANCE


def _defined(value: Any) -> bool:
    return value is not None and value != WD_UNDEFINED and value != ""


def compare_paragraph(ours: OurParagraph, theirs: WordParagraph, where: str) -> list[Difference]:
    """Diff one paragraph's resolved formatting. Skips anything Word calls
    undefined, because "mixed" is an answer our model cannot hold."""
    diffs: list[Difference] = []

    def note(prop: str, mine: Any, yours: Any) -> None:
        diffs.append(Difference(where, prop, mine, yours))

    if _defined(theirs.font_size) and ours.run.size is not None:
        if not _close(ours.run.size.points, theirs.font_size):
            note("font size", f"{ours.run.size.points:g}pt", f"{theirs.font_size:g}pt")

    if _defined(theirs.font_name) and ours.run.font_ascii:
        # Only compare when we did NOT go through the theme: a theme font
        # mismatch is question 1 in docs/open-questions.md, not a bug we can
        # assert about here.
        if ours.run.font_ascii_theme is None and ours.run.font_ascii != theirs.font_name:
            note("font", ours.run.font_ascii, theirs.font_name)

    for prop, mine, yours in (
        ("bold", ours.run.bold, theirs.bold),
        ("italic", ours.run.italic, theirs.italic),
    ):
        if _defined(yours) and mine is not None:
            # Word uses 0/-1 for False/True.
            if bool(mine) != bool(yours):
                note(prop, mine, bool(yours))

    if _defined(theirs.alignment) and ours.para.alignment is not None:
        theirs_name = ALIGNMENT.get(int(theirs.alignment))
        mine = ours.para.alignment
        # Word collapses the several justification flavours it cannot render.
        if theirs_name and mine not in (theirs_name, "justify") and mine != theirs_name:
            note("alignment", mine, theirs_name)

    for prop, mine_length, yours in (
        ("space before", ours.para.space_before, theirs.space_before),
        ("space after", ours.para.space_after, theirs.space_after),
        ("left indent", ours.para.indent_left, theirs.left_indent),
    ):
        if _defined(yours) and mine_length is not None:
            if not _close(mine_length.points, yours):
                note(prop, f"{mine_length.points:g}pt", f"{float(yours):g}pt")

    diffs.extend(_compare_line_spacing(ours, theirs, where))
    if _defined(theirs.outline_level) and ours.para.outline_level is not None:
        # Word numbers outline levels from 1; wdOutlineLevelBodyText is 10.
        theirs_level = int(theirs.outline_level)
        mine_level = ours.para.outline_level
        expected = 10 if mine_level >= 9 else mine_level + 1
        if theirs_level != expected:
            note("outline level", mine_level, theirs_level - 1)
    return diffs


def _compare_line_spacing(
    ours: OurParagraph, theirs: WordParagraph, where: str
) -> list[Difference]:
    spacing = ours.para.line_spacing
    if spacing is None or not _defined(theirs.line_spacing_rule):
        return []
    rule = int(theirs.line_spacing_rule)
    if spacing.rule == "auto":
        multiple = spacing.multiple or 1.0
        # Word stores "single" and "double" as their own rules rather than as
        # a multiple, so all three spellings have to be accepted.
        if rule == LINE_SINGLE and _close(multiple, 1.0):
            return []
        if rule == LINE_1_5 and _close(multiple, 1.5):
            return []
        if rule == LINE_DOUBLE and _close(multiple, 2.0):
            return []
        if rule == LINE_MULTIPLE and _close(multiple * 12.0, theirs.line_spacing):
            return []
        return [Difference(where, "line spacing", f"{multiple:g}x",
                           f"rule {rule} at {theirs.line_spacing}")]
    expected_rule = LINE_EXACT if spacing.rule == "exact" else LINE_AT_LEAST
    length = spacing.length
    if rule != expected_rule or (length and not _close(length.points, theirs.line_spacing)):
        return [Difference(
            where, "line spacing",
            f"{spacing.rule} {length.points if length else '?'}pt",
            f"rule {rule} at {theirs.line_spacing}",
        )]
    return []


def compare_streams(
    ours: Sequence[OurParagraph], theirs: Sequence[WordParagraph]
) -> tuple[list[Difference], str]:
    """Align on text, then diff. Returns (differences, alignment note)."""
    diffs: list[Difference] = []
    for i, (mine, yours) in enumerate(zip(ours, theirs)):
        if match_key(mine.text) != match_key(yours.text):
            return diffs, (
                f"paragraph streams diverge at paragraph {i + 1} "
                f"(we read {mine.text[:40]!r}, Word read {yours.text[:40]!r}); "
                "formatting comparison stopped there."
            )
        diffs.extend(compare_paragraph(mine, yours, f"paragraph {i + 1}"))
    note = ""
    if len(ours) != len(theirs):
        note = (
            f"we found {len(ours)} paragraphs and Word found {len(theirs)}; "
            "the shorter stream was compared."
        )
    return diffs, note


# -- the checks -----------------------------------------------------------


def run(
    document: Path,
    pdf: Path | None = None,
    timeout: float = 240.0,
    limit: int = 400,
) -> DoctorReport:
    """Open `document` in Word and check our reading of it against Word's."""
    document = Path(document)
    report = DoctorReport(document=document)
    usable, reason = available()
    if not usable:
        report.skipped_reason = reason
        return report

    pkg = OpcPackage.open(document)
    ours = read_our_paragraphs(pkg, limit)

    def work(app: Any) -> dict:
        doc = open_document(app, document)
        try:
            payload = {
                "paragraphs": read_paragraphs(doc, limit),
                "counts": {
                    "paragraphs": int(doc.Paragraphs.Count),
                    "tables": int(doc.Tables.Count),
                    "fields": int(doc.Fields.Count),
                    "footnotes": int(doc.Footnotes.Count),
                    "content_controls": int(doc.ContentControls.Count),
                    "inline_shapes": int(doc.InlineShapes.Count),
                },
                "pages": int(doc.ComputeStatistics(2)),   # wdStatisticPages
            }
            if pdf is not None:
                doc.ExportAsFixedFormat(
                    OutputFileName=str(Path(pdf).absolute()),
                    ExportFormat=WD_FORMAT_PDF,
                )
            with tempfile.TemporaryDirectory() as tmp:
                saved = Path(tmp) / "roundtrip.docx"
                doc.SaveAs2(str(saved), FileFormat=16, AddToRecentFiles=False)
                payload["roundtrip_text"] = _roundtrip_text(saved)
            return payload
        finally:
            doc.Close(WD_DO_NOT_SAVE_CHANGES)

    try:
        result = run_guarded(work, timeout=timeout)
    except WordUnavailable as exc:
        report.skipped_reason = str(exc)
        return report
    except WordTimeout as exc:
        report.checks.append(Check("word responds", False, str(exc)))
        return report
    except Exception as exc:  # noqa: BLE001 - any COM failure is a finding
        report.checks.append(Check(
            "opens without repair", False,
            f"Word could not open the document: {exc}. This is the strongest "
            "corruption signal available -- do not ship this file.",
        ))
        return report

    report.checks.append(_check_counts(pkg, ours, result))
    report.checks.append(_check_formatting(ours, result["paragraphs"]))
    report.checks.append(_check_roundtrip(ours, result.get("roundtrip_text")))
    if pdf is not None:
        report.checks.append(Check(
            "pdf export", Path(pdf).exists(),
            f"{result['pages']} page(s) -> {pdf}",
        ))
    return report


def _check_counts(pkg: OpcPackage, ours: list[OurParagraph], result: dict) -> Check:
    counts = result["counts"]
    diffs: list[Difference] = []
    ours_count = len(ours)
    if abs(ours_count - counts["paragraphs"]) > 0:
        diffs.append(Difference(
            "document", "paragraph count", ours_count, counts["paragraphs"]
        ))
    detail = (
        f"{counts['paragraphs']} paragraphs, {counts['tables']} tables, "
        f"{counts['fields']} fields, {counts['footnotes']} footnotes, "
        f"{counts['content_controls']} content controls, "
        f"{result['pages']} pages"
    )
    return Check("Word sees what we see", not diffs, detail, diffs)


def _check_formatting(ours: list[OurParagraph], theirs: list[WordParagraph]) -> Check:
    diffs, note = compare_streams(ours, theirs)
    compared = min(len(ours), len(theirs))
    detail = f"{compared} paragraphs compared"
    if note:
        detail += f"; {note}"
    return Check("our style resolver agrees with Word", not diffs, detail, diffs)


def _roundtrip_text(saved: Path) -> list[str]:
    pkg = OpcPackage.open(saved)
    return [
        match_key(b.text) for b in Walker(pkg).blocks(include_aux=False)
        if b.is_paragraph and b.text.strip()
    ]


def _check_roundtrip(ours: list[OurParagraph], theirs: list[str] | None) -> Check:
    if theirs is None:
        return Check("survives a Word save", None, "not attempted")
    mine = [match_key(p.text) for p in ours if p.text.strip()]
    if mine == theirs:
        return Check("survives a Word save", True, f"{len(mine)} paragraphs unchanged")
    lost = [t for t in mine if t not in theirs]
    return Check(
        "survives a Word save", False,
        f"{len(mine)} paragraphs in, {len(theirs)} out",
        [Difference("document", "text", t[:60], "<missing>") for t in lost[:10]],
    )
