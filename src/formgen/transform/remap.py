"""Rewriting the document-local identifiers that a graft invalidates.

Grafting the donor's `styles.xml` and `numbering.xml` onto a foreign document
replaces both id spaces wholesale, so every reference the body makes has to be
re-pointed or it dangles. Two id spaces, two different problems:

**Style ids** join across documents on the style's *name*, never its id --
`w:styleId` is localised for built-ins (`Uberschrift1` in a German Word) while
`w:name` stays English. A source style with no counterpart by name is left for
the classifier to re-style by role; a character style with no counterpart has
its `w:rStyle` dropped, because a dangling reference renders as Word's own
built-in definition, which varies by version and locale.

**List ids are meaningless across documents.** They join on the canonical
level signature instead. And a merge here has a trap of its own: two separate
numbered lists in the source that both match the same donor definition must
NOT be pointed at the same `w:num`, or the second list continues the first's
numbering instead of restarting at 1. Each numbered list therefore gets its own
`w:num` with a `w:startOverride`, and a freshly generated `w:nsid` -- a
duplicate nsid makes Word merge two unrelated lists, which is a bug that ships
in nearly every docx generator.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field

from lxml import etree

from ..oox.numbering import Numbering
from ..oox.styles import StyleGraph, normalize_style_name
from ..opc.ns import qn

STORY_STYLE_TAGS = ("w:pStyle", "w:rStyle", "w:tblStyle")


@dataclass
class StyleMap:
    """source styleId -> donor styleId, plus what could not be matched."""

    by_id: dict[str, str] = field(default_factory=dict)
    unmatched: dict[str, str] = field(default_factory=dict)   # id -> name
    donor_default: str | None = None

    def get(self, style_id: str | None) -> str | None:
        return self.by_id.get(style_id) if style_id else None


def build_style_map(source: StyleGraph, donor: StyleGraph) -> StyleMap:
    mapping = StyleMap()
    default = donor.default_paragraph_style()
    mapping.donor_default = default.style_id if default else None
    for style in source.styles.values():
        match = donor.by_name(style.name, style.type)
        if match is None:
            for alias in style.aliases:
                match = donor.by_name(alias, style.type)
                if match is not None:
                    break
        if match is not None:
            mapping.by_id[style.style_id] = match.style_id
        else:
            mapping.unmatched[style.style_id] = normalize_style_name(style.name)
    return mapping


def apply_style_map(
    root: etree._Element, mapping: StyleMap, drop_unmatched_runs: bool = True
) -> int:
    """Re-point every style reference in one story part."""
    changed = 0
    for tag in STORY_STYLE_TAGS:
        for element in list(root.findall(f".//{qn(tag)}")):
            value = element.get(qn("w:val"))
            if not value:
                continue
            target = mapping.by_id.get(value)
            if target is not None:
                if target != value:
                    element.set(qn("w:val"), target)
                    changed += 1
                continue
            if tag == "w:rStyle" and drop_unmatched_runs:
                # A dangling rStyle renders with Word's built-in definition of
                # whatever that id happens to mean in this locale. Removing it
                # is the predictable option.
                parent = element.getparent()
                if parent is not None:
                    parent.remove(element)
                    changed += 1
    return changed


# -- numbering ------------------------------------------------------------


@dataclass
class NumberingMerge:
    root: etree._Element
    num_map: dict[int, int] = field(default_factory=dict)
    added: int = 0
    reused: int = 0


def _ints(root: etree._Element, tag: str, attr: str) -> set[int]:
    out: set[int] = set()
    for element in root.findall(qn(tag)):
        raw = element.get(qn(attr))
        if raw and raw.lstrip("-").isdigit():
            out.add(int(raw))
    return out


def _fresh_nsid(seed: str, used: set[str]) -> str:
    """Deterministic, unique, and eight hex digits as Word writes them.

    Deterministic because a profile that produces a different file on every
    run cannot be diffed; unique because a shared nsid makes Word merge two
    lists that have nothing to do with each other.
    """
    for salt in range(64):
        digest = hashlib.sha1(f"{seed}#{salt}".encode()).hexdigest()[:8].upper()
        if digest not in used:
            used.add(digest)
            return digest
    raise RuntimeError("could not mint a unique w:nsid")   # pragma: no cover


def merge_numbering(
    donor_root: etree._Element | None,
    source: Numbering,
    used_num_ids: set[int],
    donor: Numbering | None = None,
) -> NumberingMerge:
    """Carry the source's lists into the donor's numbering.xml.

    Only lists the source actually uses are carried; an unused definition is
    noise that would outlive the document it came from.
    """
    root = (copy.deepcopy(donor_root) if donor_root is not None
            else etree.Element(qn("w:numbering")))
    donor = donor or Numbering.parse(root)
    merge = NumberingMerge(root=root)

    by_signature: dict[tuple, int] = {}
    for num_id in sorted(donor.instances):
        signature = donor.signature(num_id)
        if signature:
            by_signature.setdefault(signature, num_id)

    next_num = max(_ints(root, "w:num", "w:numId") | {0}) + 1
    next_abstract = max(_ints(root, "w:abstractNum", "w:abstractNumId") | {-1}) + 1
    nsids = {
        (el.get(qn("w:val")) or "").upper()
        for abstract in root.findall(qn("w:abstractNum"))
        for el in abstract.findall(qn("w:nsid"))
    }

    for num_id in sorted(used_num_ids):
        signature = source.signature(num_id)
        if signature is None:
            continue
        level = source.level(num_id, 0)
        numbered = bool(level and not level.is_bullet)
        donor_num = by_signature.get(signature)

        if donor_num is not None and not numbered:
            # Bullets carry no counter, so sharing one definition is free.
            merge.num_map[num_id] = donor_num
            merge.reused += 1
            continue

        if donor_num is not None:
            abstract_id = _abstract_of(root, donor_num)
            merge.reused += 1
        else:
            abstract_id = _import_abstract(
                root, source, num_id, next_abstract, nsids
            )
            next_abstract += 1
            merge.added += 1

        if abstract_id is None:
            continue
        merge.num_map[num_id] = _append_num(
            root, next_num, abstract_id, restart=numbered
        )
        next_num += 1
    return merge


def _abstract_of(root: etree._Element, num_id: int) -> int | None:
    for element in root.findall(qn("w:num")):
        if element.get(qn("w:numId")) == str(num_id):
            target = element.find(qn("w:abstractNumId"))
            raw = target.get(qn("w:val")) if target is not None else None
            if raw and raw.lstrip("-").isdigit():
                return int(raw)
    return None


def _import_abstract(
    root: etree._Element, source: Numbering, num_id: int,
    abstract_id: int, nsids: set[str],
) -> int | None:
    abstract = source.abstract_for(num_id)
    if abstract is None:
        return None
    element = source.element_for_abstract(abstract.abstract_id)
    if element is None:
        return None

    copied = copy.deepcopy(element)
    copied.set(qn("w:abstractNumId"), str(abstract_id))
    for nsid in copied.findall(qn("w:nsid")):
        copied.remove(nsid)
    nsid = etree.Element(qn("w:nsid"))
    nsid.set(qn("w:val"), _fresh_nsid(repr(source.signature(num_id)), nsids))
    copied.insert(0, nsid)
    # Schema order: numPicBullet*, abstractNum*, num*. Inserting an
    # abstractNum after the first w:num produces a file Word repairs.
    nums = root.findall(qn("w:num"))
    if nums:
        nums[0].addprevious(copied)
    else:
        root.append(copied)
    return abstract_id


def _append_num(
    root: etree._Element, num_id: int, abstract_id: int, restart: bool
) -> int:
    element = etree.SubElement(root, qn("w:num"))
    element.set(qn("w:numId"), str(num_id))
    target = etree.SubElement(element, qn("w:abstractNumId"))
    target.set(qn("w:val"), str(abstract_id))
    if restart:
        # Without this, a second numbered list that shares a definition
        # continues the first one's numbering instead of restarting at 1.
        override = etree.SubElement(element, qn("w:lvlOverride"))
        override.set(qn("w:ilvl"), "0")
        start = etree.SubElement(override, qn("w:startOverride"))
        start.set(qn("w:val"), "1")
    return num_id


def apply_num_map(root: etree._Element, num_map: dict[int, int]) -> int:
    """Re-point every w:numId in one story part, leaving 0 alone."""
    changed = 0
    for element in root.findall(f".//{qn('w:numId')}"):
        raw = element.get(qn("w:val"))
        if not raw or not raw.lstrip("-").isdigit():
            continue
        value = int(raw)
        if value == 0:
            # "Explicitly not numbered" is not a reference to anything.
            continue
        target = num_map.get(value)
        if target is not None and target != value:
            element.set(qn("w:val"), str(target))
            changed += 1
    return changed
