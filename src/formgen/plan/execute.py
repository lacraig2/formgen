"""Executing a plan: the only code in the project allowed to change a document.

The order below is not arbitrary. Identifier maps are computed **before** the
graft, because computing them afterwards would be reading the donor's ids out
of a package that is already the donor's. The graft then replaces the format
parts, and only then are the body's references re-pointed and its direct
formatting stripped.

Nothing is written to disk here. `execute` mutates an in-memory package and
returns; the caller runs the invariant suite and decides whether the result
may be written at all. That separation is what makes "verify before writing"
enforceable rather than a convention.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

from lxml import etree

from ..oox.numbering import Numbering
from ..oox.props import ParaProps
from ..oox.styles import StyleGraph, normalize_style_name
from ..oox.theme import Theme
from ..oox.walk import Walker
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from ..transform.graft import GraftReport, graft
from ..transform.remap import (
    apply_num_map, apply_style_map, build_style_map, merge_numbering,
)
from ..transform.strip import StripStats, keep_set, strip_paragraph, strip_runs
from .model import Plan, SetPStyle

COMMENTS_CT = ("application/vnd.openxmlformats-officedocument."
               "wordprocessingml.comments+xml")

# What to do with a classification we are not confident about.
KEEP, RESTYLE = "keep", "restyle"


@dataclass
class ApplyReport:
    edits_applied: int = 0
    edits_skipped: int = 0
    strip: StripStats = field(default_factory=StripStats)
    style_refs_remapped: int = 0
    num_refs_remapped: int = 0
    lists_added: int = 0
    lists_reused: int = 0
    unmatched_styles: list[str] = field(default_factory=list)
    comments_added: int = 0
    graft: GraftReport = field(default_factory=GraftReport)

    def summary(self) -> str:
        return (
            f"{self.edits_applied} block(s) restyled, "
            f"{self.edits_skipped} left for review; "
            f"{self.strip.properties_removed} direct properties removed, "
            f"{self.strip.emphasis_kept} emphasis spans kept"
        )


def _story_parts(pkg: OpcPackage) -> list[str]:
    parts = [pkg.main_document]
    for reltype in ("footnotes", "endnotes", "header", "footer"):
        parts.extend(p for p in dict.fromkeys(pkg.related_all(RT[reltype]))
                     if p in pkg)
    return list(dict.fromkeys(parts))


def _graph(pkg: OpcPackage) -> StyleGraph:
    part = pkg.related(RT["styles"])
    theme_part = pkg.related(RT["theme"])
    theme = Theme.parse(
        pkg.element(theme_part) if theme_part and theme_part in pkg else None
    )
    if part and part in pkg:
        return StyleGraph.parse(pkg.element(part), theme)
    from ..oox.props import RunProps

    return StyleGraph({}, RunProps(), ParaProps(), theme)


def _numbering(pkg: OpcPackage, styles: StyleGraph) -> Numbering:
    part = pkg.related(RT["numbering"])
    return Numbering.parse(
        pkg.element(part) if part and part in pkg else None, styles
    )


def _used_lists(pkg: OpcPackage, styles: StyleGraph) -> set[int]:
    used: set[int] = set()
    for block in Walker(pkg).blocks():
        if not block.is_paragraph:
            continue
        direct = ParaProps.parse(block.element.find(qn("w:pPr")))
        props = styles.effective_for_para(
            styles.para_style_or_default(block.style_id), direct
        )
        if props.num_id:
            used.add(props.num_id)
    return used


def execute(
    target: OpcPackage,
    donor: OpcPackage,
    plan: Plan,
    on_low_confidence: str = KEEP,
    mark_uncertain: bool = False,
) -> ApplyReport:
    """Reformat `target` in place to match `donor`, following `plan`."""
    report = ApplyReport()

    source_styles = _graph(target)
    donor_styles = _graph(donor)
    source_numbering = _numbering(target, source_styles)
    donor_numbering = _numbering(donor, donor_styles)

    style_map = build_style_map(source_styles, donor_styles)
    report.unmatched_styles = sorted(set(style_map.unmatched.values()))
    donor_numbering_part = donor.related(RT["numbering"])
    merge = merge_numbering(
        donor.element(donor_numbering_part)
        if donor_numbering_part and donor_numbering_part in donor else None,
        source_numbering,
        _used_lists(target, source_styles),
        donor_numbering,
    )
    report.lists_added, report.lists_reused = merge.added, merge.reused

    report.graft = graft(target, donor, numbering_root=merge.root)

    for part in _story_parts(target):
        root = target.edit(part)
        report.style_refs_remapped += apply_style_map(root, style_map)
        report.num_refs_remapped += apply_num_map(root, merge.num_map)

    _apply_edits(target, donor_styles, plan, on_low_confidence, report)
    if mark_uncertain:
        report.comments_added = _mark_uncertain(target, plan)
    return report


def _apply_edits(
    target: OpcPackage, donor_styles: StyleGraph, plan: Plan,
    on_low_confidence: str, report: ApplyReport,
) -> None:
    blocks = {b.path: b for b in Walker(target).blocks()}
    by_name = {
        normalize_style_name(s.name): s.style_id
        for s in donor_styles.styles.values()
        if s.type == "paragraph"
    }

    for edit in plan.edits:
        block = blocks.get(edit.path)
        if block is None or not block.is_paragraph:
            continue
        if edit.needs_review and on_low_confidence == KEEP:
            # Leaving a guess alone is always recoverable; acting on one is
            # not. The review sidecar names every paragraph skipped here.
            report.edits_skipped += 1
            continue

        style_id = None
        for op in edit.ops:
            if isinstance(op, SetPStyle):
                style_id = by_name.get(normalize_style_name(op.style_name))
                break
        if style_id is None:
            style_id = _current_style(block.element)

        numbered = block.element.find(
            f"{qn('w:pPr')}/{qn('w:numPr')}"
        ) is not None
        strip_paragraph(
            block.element, style_id, keep_set(block.element, numbered), report.strip
        )
        strip_runs(block.element, report.strip)
        report.edits_applied += 1


def _current_style(paragraph: etree._Element) -> str | None:
    element = paragraph.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    return element.get(qn("w:val")) if element is not None else None


# -- the review loop ------------------------------------------------------


def _mark_uncertain(target: OpcPackage, plan: Plan) -> int:
    """Anchor a real Word comment on each paragraph we were unsure about.

    With Word on the machine this turns reviewing into pressing Next Comment,
    instead of hunting for find strings in a report. That is the difference
    between a review loop people use and one they do not.
    """
    uncertain = plan.needs_review
    if not uncertain:
        return 0
    blocks = {b.path: b for b in Walker(target).blocks()}
    root, part = _comments_part(target)
    next_id = _next_comment_id(root)
    added = 0
    stamp = _dt.datetime.now().replace(microsecond=0).isoformat()

    for edit in uncertain:
        block = blocks.get(edit.path)
        if block is None or not block.is_paragraph:
            continue
        text = (
            f"formgen read this as {edit.role} "
            f"(confidence {edit.confidence:.2f}) and left it alone. "
            + "; ".join(edit.evidence[:2])
        )
        _append_comment(root, next_id, text, stamp)
        _anchor_comment(block.element, next_id)
        next_id += 1
        added += 1

    target.replace_part(part, etree.tostring(
        root, xml_declaration=True, encoding="UTF-8", standalone=True
    ))
    return added


def _comments_part(pkg: OpcPackage) -> tuple[etree._Element, str]:
    existing = pkg.related(RT["comments"])
    if existing and existing in pkg:
        return pkg.element(existing), existing
    import posixpath

    name = posixpath.join(posixpath.dirname(pkg.main_document), "comments.xml")
    root = etree.Element(qn("w:comments"), nsmap={"w": qn("w:x")[1:-2]})
    pkg.add_part(name, etree.tostring(root, xml_declaration=True,
                                      encoding="UTF-8", standalone=True),
                 COMMENTS_CT)
    pkg.relate(RT["comments"], name, pkg.main_document)
    return pkg.element(name), name


def _next_comment_id(root: etree._Element) -> int:
    ids = [
        int(raw) for el in root.findall(qn("w:comment"))
        if (raw := el.get(qn("w:id"))) and raw.lstrip("-").isdigit()
    ]
    return max(ids) + 1 if ids else 1


def _append_comment(root: etree._Element, cid: int, text: str, stamp: str) -> None:
    comment = etree.SubElement(root, qn("w:comment"))
    comment.set(qn("w:id"), str(cid))
    comment.set(qn("w:author"), "formgen")
    comment.set(qn("w:initials"), "fg")
    comment.set(qn("w:date"), stamp)
    paragraph = etree.SubElement(comment, qn("w:p"))
    mark = etree.SubElement(paragraph, qn("w:r"))
    etree.SubElement(mark, qn("w:annotationRef"))
    run = etree.SubElement(paragraph, qn("w:r"))
    node = etree.SubElement(run, qn("w:t"))
    node.set(qn("xml:space"), "preserve")
    node.text = text


def _anchor_comment(paragraph: etree._Element, cid: int) -> None:
    start = etree.Element(qn("w:commentRangeStart"))
    start.set(qn("w:id"), str(cid))
    ppr = paragraph.find(qn("w:pPr"))
    paragraph.insert(1 if ppr is not None else 0, start)

    end = etree.SubElement(paragraph, qn("w:commentRangeEnd"))
    end.set(qn("w:id"), str(cid))
    run = etree.SubElement(paragraph, qn("w:r"))
    reference = etree.SubElement(run, qn("w:commentReference"))
    reference.set(qn("w:id"), str(cid))
