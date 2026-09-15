"""Removing direct formatting without removing meaning.

The whole difficulty is that direct formatting and emphasis are the same XML.
``<w:b/>`` on a run is a decision to make something bold; whether that was a
formatting decision or a writing decision depends entirely on what it covers:

    If a run property covers **the entire paragraph**, it is presentational
    -> drop it. If it covers a **proper sub-span**, it is emphasis -> keep it.

That one rule keeps "the *p*-value was significant" and discards
"**A WHOLE BOLD HEADING**", which is exactly the distinction a person would
make and is not expressible any other way.

Two structural invariants hold throughout:

* **Runs are never rebuilt.** Only ``w:pPr`` and each run's ``w:rPr`` are
  edited, in place. That single constraint is what keeps fields, bookmarks,
  comment ranges, OMML and tracked changes intact -- every docx tool that
  reassembles a paragraph's runs loses some of them.
* **w:rStyle is remapped, never stripped.** It is how Hyperlink,
  FootnoteReference and CommentReference survive a reformat; dropping it turns
  every link in the document black and unlined.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import NS, qn

# Kept when, and only when, it covers a proper sub-span of the paragraph.
EMPHASIS = ("w:b", "w:bCs", "w:i", "w:iCs", "w:u", "w:strike", "w:dstrike")

# Semantic, never presentational: dropping these changes what the text says.
SEMANTIC = (
    "w:rStyle",       # remapped elsewhere -- see remap.py
    "w:vertAlign",    # superscript/subscript is notation, not styling
    "w:lang",         # spell-check and screen readers use it
    "w:rtl", "w:cs", "w:em", "w:eastAsianLayout",
    "w:specVanish", "w:oMath", "w:noProof",
)

# Always presentational.
PRESENTATIONAL = (
    "w:rFonts", "w:sz", "w:szCs", "w:color", "w:highlight", "w:shd",
    "w:spacing", "w:position", "w:kern", "w:bdr", "w:effect", "w:outline",
    "w:shadow", "w:emboss", "w:imprint", "w:caps", "w:smallCaps", "w:w",
    "w:fitText", "w:snapToGrid", "w:webHidden",
)

# w:pPr children we may keep, in the order CT_PPr requires. Writing them in
# any other order produces a file Word offers to repair.
_PPR_ORDER = (
    "w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore",
    "w:numPr", "w:tabs", "w:rPr", "w:sectPr",
)


@dataclass
class StripStats:
    runs_touched: int = 0
    properties_removed: int = 0
    emphasis_kept: int = 0
    paragraph_props_removed: int = 0
    kept: list[str] = field(default_factory=list)


def _text_runs(paragraph: etree._Element) -> list[etree._Element]:
    """Runs that carry visible text, excluding the paragraph mark's own rPr.

    A run holding only a bookmark or a field character has no appearance to
    strip, and counting it would make every property look like a sub-span.
    """
    out = []
    for run in paragraph.iter(qn("w:r")):
        parent = run.getparent()
        if parent is not None and parent.tag == qn("w:pPr"):
            continue
        if run.find(qn("w:t")) is not None or run.find(qn("w:tab")) is not None:
            out.append(run)
    return out


def _has(run: etree._Element, tag: str) -> bool:
    rpr = run.find(qn("w:rPr"))
    return rpr is not None and rpr.find(qn(tag)) is not None


def span_scope(paragraph: etree._Element, tag: str) -> str:
    """"all", "some" or "none" -- how much of the paragraph this property covers."""
    runs = _text_runs(paragraph)
    if not runs:
        return "none"
    carrying = sum(1 for run in runs if _has(run, tag))
    if carrying == 0:
        return "none"
    return "all" if carrying == len(runs) else "some"


def strip_runs(paragraph: etree._Element, stats: StripStats | None = None) -> StripStats:
    """Apply the span-scope rule to every run of one paragraph."""
    stats = stats or StripStats()
    scopes = {tag: span_scope(paragraph, tag) for tag in EMPHASIS}

    for run in _text_runs(paragraph) or list(paragraph.iter(qn("w:r"))):
        rpr = run.find(qn("w:rPr"))
        if rpr is None:
            continue
        parent = rpr.getparent()
        if parent is not None and parent.tag == qn("w:pPr"):
            continue
        stats.runs_touched += 1
        for child in list(rpr):
            if not isinstance(child.tag, str):
                continue
            name = _name_of(child)
            if name in SEMANTIC:
                continue
            if name in EMPHASIS:
                if scopes.get(name) == "some":
                    stats.emphasis_kept += 1
                    continue
                rpr.remove(child)
                stats.properties_removed += 1
                continue
            # Everything else goes. Enumerating what to KEEP is the only
            # way to be right about a foreign document: a pPr/rPr accumulates
            # a long tail of properties nobody chose, and a removal list will
            # always be missing the one you have not seen yet.
            rpr.remove(child)
            stats.properties_removed += 1
        if len(rpr) == 0:
            run.remove(rpr)
    return stats


def _name_of(element: etree._Element) -> str:
    """'w:b' for WordprocessingML, a Clark name for anything else."""
    qname = etree.QName(element)
    if qname.namespace == NS["w"]:
        return f"w:{qname.localname}"
    return f"{{{qname.namespace}}}{qname.localname}"


def strip_paragraph(
    paragraph: etree._Element,
    style_id: str | None = None,
    keep: tuple[str, ...] = ("sectPr", "pageBreakBefore"),
    stats: StripStats | None = None,
) -> StripStats:
    """Replace w:pPr with a fresh one carrying only what we decided to keep.

    Rebuilding rather than pruning is deliberate: a foreign document's pPr
    accumulates a long tail of properties nobody chose, and enumerating what
    to remove means missing the one you have not seen yet. Enumerating what to
    keep cannot.

    ``sectPr`` is always kept -- dropping it merges two sections and silently
    changes the page setup of everything before it. ``tabs`` is kept only when
    the paragraph's text actually contains a tab, because signature blocks and
    leader-dot lines break catastrophically without their stops and every
    other paragraph is better off with the style's.
    """
    stats = stats or StripStats()
    ppr = paragraph.find(qn("w:pPr"))
    keepers: list[etree._Element] = []

    if ppr is not None:
        for name in keep:
            for child in ppr.findall(qn(f"w:{name}")):
                keepers.append(child)
                stats.kept.append(name)
        num_pr = ppr.find(qn("w:numPr"))
        if num_pr is not None and "numPr" in keep:
            keepers.append(num_pr)
        stats.paragraph_props_removed += sum(
            1 for c in ppr if isinstance(c.tag, str) and c not in keepers
        )
        paragraph.remove(ppr)

    fresh = etree.Element(qn("w:pPr"))
    if style_id:
        style = etree.SubElement(fresh, qn("w:pStyle"))
        style.set(qn("w:val"), style_id)
    for child in keepers:
        fresh.append(child)
    _order(fresh)
    if len(fresh):
        paragraph.insert(0, fresh)
    return stats


def _order(ppr: etree._Element) -> None:
    index = {qn(tag): i for i, tag in enumerate(_PPR_ORDER)}
    children = sorted(ppr, key=lambda c: index.get(c.tag, len(index)))
    for child in children:
        ppr.append(child)


def has_tab(paragraph: etree._Element) -> bool:
    """Does the paragraph's TEXT contain a tab character?

    Not the same as having tab stops. `w:tab` means two different things
    depending on where it sits: inside `w:tabs` it is a CT_TabStop -- a
    definition -- and inside `w:r` it is the character itself. Matching
    either way round keeps the stops for every paragraph that merely defines
    some, which is most of them, and the rule stops discriminating.
    """
    return paragraph.find(f".//{qn('w:r')}/{qn('w:tab')}") is not None


def keep_set(paragraph: etree._Element, numbered: bool = False) -> tuple[str, ...]:
    """What to preserve for one paragraph, decided from the paragraph itself."""
    keep = ["sectPr", "pageBreakBefore"]
    if has_tab(paragraph):
        keep.append("tabs")
    if numbered:
        keep.append("numPr")
    return tuple(keep)
