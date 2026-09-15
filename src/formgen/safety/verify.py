"""Structural invariants -- the same code in the tests and in production.

This is the last thing that runs before any output is written. If an invariant
fails, nothing is written at all and the failure names what broke. That rule
is absolute: a tool that rewrites other people's documents may produce a file
that is not clean enough, but it may never produce one that is missing their
content.

The invariants are chosen to catch the failures that are *silent*. Word will
tell you loudly about a malformed package; it will say nothing at all when a
hyperlink has lost its target, a comment anchor points at a comment that is no
longer there, or six paragraphs of a text box have quietly disappeared --
because every one of those still opens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from ..oox.walk import Walker, match_key
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage

# Things whose count must never change across a reformat. Each is a distinct
# way a document can quietly lose something a reader would notice.
COUNTED = {
    "hyperlinks": "w:hyperlink",
    "footnote references": "w:footnoteReference",
    "endnote references": "w:endnoteReference",
    "comment anchors": "w:commentReference",
    "images": "a:blip",
    "equations": "m:oMath",
    "embedded objects": "w:object",
    "content controls": "w:sdt",
    "bookmarks": "w:bookmarkStart",
    "fields": "w:fldChar",
    "drawings": "w:drawing",
    "tables": "w:tbl",
}


@dataclass
class Snapshot:
    """What a document contained, in the terms the invariants are stated in."""

    text: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    paragraphs: int = 0
    parts: set[str] = field(default_factory=set)
    chrome_text: list[str] = field(default_factory=list)
    chrome_counts: dict[str, int] = field(default_factory=dict)

    @property
    def joined(self) -> str:
        return "\n".join(self.text)


def _content_parts(pkg: OpcPackage) -> list[str]:
    """Parts holding the author's content -- what must survive unchanged."""
    parts = [pkg.main_document]
    for reltype in ("footnotes", "endnotes", "comments"):
        for part in dict.fromkeys(pkg.related_all(RT[reltype])):
            if part in pkg:
                parts.append(part)
    return list(dict.fromkeys(parts))


def _chrome_parts(pkg: OpcPackage) -> list[str]:
    """Headers and footers -- the format's, not the author's.

    A graft replaces these wholesale and deliberately, so counting their text
    as the document's own would report every successful reformat as having
    invented a paragraph.
    """
    parts = []
    for reltype in ("header", "footer"):
        parts.extend(p for p in dict.fromkeys(pkg.related_all(RT[reltype]))
                     if p in pkg)
    return list(dict.fromkeys(parts))


def _story_parts(pkg: OpcPackage) -> list[str]:
    """Every part that holds visible text, for integrity checks."""
    return _content_parts(pkg) + _chrome_parts(pkg)


def snapshot(pkg: OpcPackage) -> Snapshot:
    """Measure a package before and after, for comparison."""
    chrome = set(_chrome_parts(pkg))
    counts = {name: 0 for name in COUNTED}
    chrome_counts = {name: 0 for name in COUNTED}
    for part in _content_parts(pkg):
        root = pkg.element(part)
        for name, tag in COUNTED.items():
            counts[name] += len(root.findall(f".//{qn(tag)}"))
    for part in chrome:
        root = pkg.element(part)
        for name, tag in COUNTED.items():
            chrome_counts[name] += len(root.findall(f".//{qn(tag)}"))

    text: list[str] = []
    chrome_text: list[str] = []
    for block in Walker(pkg).blocks():
        # Whitespace-only paragraphs are excluded by design: normalising
        # spacing is a legitimate thing for a reformat to do, and a blank
        # paragraph carries nothing a reader would miss.
        if not block.is_paragraph or block.context.in_del or not block.text.strip():
            continue
        target = chrome_text if block.context.part in chrome else text
        target.append(match_key(block.text))
    return Snapshot(
        text=text, counts=counts, paragraphs=len(text), parts=set(pkg.names()),
        chrome_text=chrome_text, chrome_counts=chrome_counts,
    )


def compare(
    before: Snapshot, after: Snapshot, allowed_gains: dict[str, int] | None = None
) -> list[str]:
    """Every way the output lost something the input had.

    `allowed_gains` records additions we made on purpose -- review comments,
    a grafted header's logo -- so that a deliberate change is not reported as
    corruption while an accidental one still is.
    """
    allowed_gains = allowed_gains or {}
    faults: list[str] = []
    if before.text != after.text:
        faults.extend(_text_faults(before, after))
    for name, was in sorted(before.counts.items()):
        now = after.counts.get(name, 0)
        if now < was:
            faults.append(f"lost {was - now} of {was} {name}")
        elif now > was + allowed_gains.get(name, 0):
            # Gaining one is usually harmless (a grafted header brings its
            # own images), but it is never intentional in the body, so it is
            # reported rather than ignored.
            faults.append(f"gained {now - was} {name} (was {was}, now {now})")
    return faults


def _text_faults(before: Snapshot, after: Snapshot) -> list[str]:
    missing = [t for t in before.text if t not in set(after.text)]
    added = [t for t in after.text if t not in set(before.text)]
    faults = []
    if missing:
        sample = "; ".join(t[:50] for t in missing[:3])
        faults.append(f"lost {len(missing)} paragraph(s) of text: {sample}")
    if added:
        sample = "; ".join(t[:50] for t in added[:3])
        faults.append(f"invented {len(added)} paragraph(s) of text: {sample}")
    if not faults and before.text != after.text:
        faults.append(
            f"the text is the same but reordered "
            f"({before.paragraphs} paragraphs)"
        )
    return faults


# -- referential integrity ------------------------------------------------


def check_integrity(pkg: OpcPackage) -> list[str]:
    """Every reference resolves. This is what Word's repair prompt reacts to."""
    faults: list[str] = []
    faults.extend(
        f"{source}: relationship {rid} points at {target!r}, which is not in "
        "the package"
        for source, rid, target in pkg.dangling_rels()
    )
    faults.extend(_check_rid_references(pkg))
    faults.extend(_check_style_references(pkg))
    faults.extend(_check_numbering_references(pkg))
    faults.extend(_check_bookmarks(pkg))
    faults.extend(_check_sectpr_placement(pkg))
    faults.extend(pkg.content_types.validate(pkg.names()))
    return faults


# Attributes that carry a relationship id, and must resolve in THAT part's
# rels -- rIds are part-local, so a body rId means nothing in a header.
_RID_ATTRS = ("r:id", "r:embed", "r:link", "r:pict", "r:dm", "r:lo", "r:qs", "r:cs")


def _check_rid_references(pkg: OpcPackage) -> list[str]:
    faults: list[str] = []
    for part in _story_parts(pkg):
        rels = pkg.rels(part)
        root = pkg.element(part)
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            for attr in _RID_ATTRS:
                rid = element.get(qn(attr))
                if rid and rid not in rels:
                    faults.append(
                        f"{part}: <{etree.QName(element).localname}> references "
                        f"{rid}, which that part's relationships do not define"
                    )
    return faults


def _check_style_references(pkg: OpcPackage) -> list[str]:
    styles_part = pkg.related(RT["styles"])
    if not styles_part or styles_part not in pkg:
        return []
    defined = {
        el.get(qn("w:styleId"))
        for el in pkg.element(styles_part).findall(qn("w:style"))
    }
    faults: list[str] = []
    seen: set[tuple[str, str]] = set()
    for part in _story_parts(pkg):
        root = pkg.element(part)
        for tag in ("w:pStyle", "w:rStyle", "w:tblStyle"):
            for element in root.findall(f".//{qn(tag)}"):
                value = element.get(qn("w:val"))
                if value and value not in defined and (part, value) not in seen:
                    seen.add((part, value))
                    faults.append(
                        f"{part}: <{tag}> references style {value!r}, which "
                        "styles.xml does not define"
                    )
    return faults


def _check_numbering_references(pkg: OpcPackage) -> list[str]:
    part = pkg.related(RT["numbering"])
    faults: list[str] = []
    nums: set[int] = set()
    abstracts: set[int] = set()
    if part and part in pkg:
        root = pkg.element(part)
        for el in root.findall(qn("w:num")):
            raw = el.get(qn("w:numId"))
            if raw and raw.lstrip("-").isdigit():
                nums.add(int(raw))
            target = el.find(qn("w:abstractNumId"))
            if target is not None:
                value = target.get(qn("w:val"))
                if value and value.lstrip("-").isdigit():
                    abstracts.add(int(value))
        defined_abstracts = {
            int(el.get(qn("w:abstractNumId")))
            for el in root.findall(qn("w:abstractNum"))
            if (el.get(qn("w:abstractNumId")) or "").lstrip("-").isdigit()
        }
        for missing in sorted(abstracts - defined_abstracts):
            faults.append(
                f"numbering.xml: w:num points at abstractNumId {missing}, "
                "which is not defined"
            )

    seen: set[int] = set()
    for story in _story_parts(pkg):
        for el in pkg.element(story).findall(f".//{qn('w:numId')}"):
            raw = el.get(qn("w:val"))
            if not raw or not raw.lstrip("-").isdigit():
                continue
            value = int(raw)
            # 0 is "explicitly not numbered" and never resolves to a w:num.
            if value and value not in nums and value not in seen:
                seen.add(value)
                faults.append(
                    f"{story}: paragraph uses numId {value}, which "
                    "numbering.xml does not define"
                )
    return faults


def _check_bookmarks(pkg: OpcPackage) -> list[str]:
    faults: list[str] = []
    for part in _story_parts(pkg):
        root = pkg.element(part)
        starts = {
            el.get(qn("w:id")) for el in root.findall(f".//{qn('w:bookmarkStart')}")
        }
        ends = {
            el.get(qn("w:id")) for el in root.findall(f".//{qn('w:bookmarkEnd')}")
        }
        for orphan in sorted(x for x in starts - ends if x):
            faults.append(f"{part}: bookmark {orphan} starts but never ends")
        for orphan in sorted(x for x in ends - starts if x):
            faults.append(f"{part}: bookmark {orphan} ends but never starts")
    return faults


def _check_sectpr_placement(pkg: OpcPackage) -> list[str]:
    """The body-level w:sectPr must be the LAST child of w:body.

    A w:p after it is the single easiest way to make Word offer to repair the
    file, and it is exactly what appending content to a body does if you have
    not thought about it.
    """
    body = pkg.element(pkg.main_document).find(qn("w:body"))
    if body is None:
        return ["document.xml has no w:body"]
    children = [c for c in body if isinstance(c.tag, str)]
    positions = [i for i, c in enumerate(children) if c.tag == qn("w:sectPr")]
    if not positions:
        return ["w:body has no final w:sectPr; the page setup is undefined"]
    if len(positions) > 1:
        return [f"w:body has {len(positions)} body-level w:sectPr elements"]
    if positions[0] != len(children) - 1:
        return [
            f"{len(children) - 1 - positions[0]} element(s) follow the "
            "body-level w:sectPr; Word treats this as corruption"
        ]
    return []


# -- the production gate --------------------------------------------------


def verify(
    before: Snapshot, pkg: OpcPackage, allowed_gains: dict[str, int] | None = None
) -> list[str]:
    """Everything that must hold before an output may be written."""
    return compare(before, snapshot(pkg), allowed_gains) + check_integrity(pkg)
