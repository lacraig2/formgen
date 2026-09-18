"""Filling a PowerPoint form, the slide-side twin of :mod:`fill`.

A ``.docx`` keeps its text in one body of ``w:p`` paragraphs; a ``.pptx`` keeps
it on many slides, each a tree of DrawingML paragraphs (``a:p`` / ``a:r`` /
``a:t``) hung off shapes. The marker grammar is identical -- ``{{name}}``,
``{{check: agreed}}``, ``{{choice: s | a, b}}`` and the rest -- so the *matcher*
is shared with :mod:`fill` outright: only the walk differs.

What a presentation supports:

* **Typed text markers** -- text, date, number, check box and choice -- filled
  in place inside the run that holds them, so the marker keeps its formatting.
  A marker split across runs is pulled back together first, exactly as in Word.
* **Pictures marked in their alt text.** Right-click a picture, Edit Alt Text,
  type ``{{logo}}``: fill swaps the image inside it and leaves its size and
  position alone. This is the presentation image story -- a slide picture is a
  positioned shape, not a character in a line, so there is no sensible "drop it
  where the marker sat" the way an inline ``{{image: ...}}`` works in a body.

The two omissions relative to Word are deliberate: an inline ``{{image: ...}}``
typed into slide text (nowhere to put a floating shape) and repeating rows
(a WordprocessingML table feature). Both simply stay as literal text and are
reported as leftover, so nothing is silently dropped.
"""

from __future__ import annotations

import posixpath
from copy import deepcopy
from typing import Any

from lxml import etree

from ..learn.formfields import (
    CHECKBOX, IMAGE, _DOTTED, _MARKERS, FormField, FormReport, marker_kind, slug,
)
from ..opc.errors import PackageError
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from .fill import (
    CHECKED, UNCHECKED, FillReport, _as_text, _checked, _coalesce_markers,
    _image_bytes, _lines, _marker_inner, _match, _release_image, _sub_once,
    _text_segments, _UNAMBIGUOUS_MARKER,
)
from .imageinfo import sniff
from .images import add_image_part

_A_T = qn("a:t")
_A_R = qn("a:r")


def is_presentation(pkg: OpcPackage) -> bool:
    """Whether this package is a PresentationML (``.pptx``) document.

    Decided by the content type of the part the root relationship points at,
    not by the ``ppt/`` naming convention -- the type is normative, the path
    is not."""
    try:
        content_type = pkg.content_types.for_part(pkg.main_document) or ""
    except PackageError:
        return False
    return "presentationml.presentation" in content_type


def slide_parts(pkg: OpcPackage) -> list[str]:
    """Every slide part, in presentation order.

    Order comes from ``p:sldIdLst`` in the presentation part; any slide the
    package relates to but the list omits is appended, so a stray slide is
    still filled rather than skipped."""
    try:
        main = pkg.main_document
    except PackageError:
        return []
    root = pkg.element(main)
    ordered: list[str] = []
    lst = root.find(qn("p:sldIdLst"))
    if lst is not None:
        rels = pkg.rels(main)
        for sld in lst.findall(qn("p:sldId")):
            rid = sld.get(qn("r:id"))
            rel = rels.get(rid) if rid else None
            if rel is None or rel.external:
                continue
            target = rel.resolve(main)
            actual = pkg.actual_name(target) if target else None
            if actual and actual in pkg and actual not in ordered:
                ordered.append(actual)
    for part in pkg.related_all(RT["slide"], main):
        if part not in ordered:
            ordered.append(part)
    return ordered


# -- discovery ------------------------------------------------------------


def find_presentation_fields(pkg: OpcPackage) -> FormReport:
    """Fields a presentation declares: typed markers and marked-up pictures.

    A marker repeated across slides (a footer field, say) is one field, not
    one per slide, so the same value fills every occurrence -- the opposite of
    Word's positional de-duplication, and the right behaviour for a deck."""
    report = FormReport()
    seen: set[str] = set()

    def emit(item: FormField) -> None:
        if not item.name or item.name in seen:
            return
        seen.add(item.name)
        report.fields.append(item)

    for slide in slide_parts(pkg):
        root = pkg.element(slide)
        for index, a_p in enumerate(root.iter(qn("a:p"))):
            _markers_of(_paragraph_text(a_p), slide, index, emit)
        for pic in root.iter(qn("p:pic")):
            _picture_field(pic, slide, emit)
    return report


def _paragraph_text(a_p: etree._Element) -> str:
    return _text_segments(a_p, _A_T, _A_R)[0]


def _markers_of(text: str, slide: str, index: int, emit) -> None:
    ordinal = 0
    for style, pattern in _MARKERS:
        for match in pattern.finditer(text):
            marker = marker_kind(match.group(1).strip())
            if not marker.name or len(marker.name.split()) > 6:
                continue
            if _DOTTED.match(marker.name):
                continue                     # no repeating rows on slides
            if marker.kind == IMAGE:
                continue                     # images come from pictures, below
            emit(FormField(
                kind=marker.kind, source="marker", raw_name=match.group(0),
                name=slug(marker.name), name_confidence=0.50,
                name_source=f"a {style} marker on a slide",
                label=marker.label, block_path=f"{slide}#p{index}",
                ordinal=ordinal, choices=marker.choices,
                required=marker.required, needs_review=True,
            ))
            ordinal += 1


def _picture_field(pic: etree._Element, slide: str, emit) -> None:
    inner = _picture_alt_marker(pic)
    if inner is None:
        return
    marker = marker_kind(inner.strip())
    emit(FormField(
        kind=IMAGE, source="picture", raw_name=inner[:60],
        name=slug(marker.name), name_confidence=0.90,
        name_source="a picture's alt text", label=marker.label,
        block_path=slide, required=marker.required,
    ))


def _picture_alt_marker(pic: etree._Element) -> str | None:
    """The marker written in a slide picture's alt text, or None.

    PowerPoint stores alt text on the picture's ``p:cNvPr`` (description and
    title). Bracket markers are excluded: alt text is often prose, and a stray
    ``[note]`` should not silently become a field."""
    for cnv in pic.iter(qn("p:cNvPr")):
        for text in (cnv.get("descr") or "", cnv.get("title") or ""):
            for name, pattern in _MARKERS:
                if name == "bracket":
                    continue
                hit = pattern.search(text)
                if hit is not None:
                    return hit.group(1)
    return None


# -- fill -----------------------------------------------------------------


def fill_presentation(pkg: OpcPackage, values: dict[str, Any],
                      keep_unsupplied: bool = False) -> FillReport:
    """Write `values` into every slide of `pkg`, in place.

    Only the slides that actually change are marked dirty, so an untouched
    slide is written back byte-for-byte -- the same preservation guarantee the
    body fill relies on, applied per slide."""
    report = FillReport()
    wanted = {slug(k): v for k, v in values.items()}
    seen: set[str] = set()
    for slide in slide_parts(pkg):
        root = pkg.element(slide)
        changed = _fill_slide_text(root, wanted, seen, report)
        changed = _swap_slide_pictures(
            pkg, slide, root, wanted, seen, report, keep_unsupplied) or changed
        if changed:
            pkg.touch(slide)
    report.unknown = sorted(set(wanted) - seen)
    report.leftover = _leftover(pkg)
    return report


def _fill_slide_text(root: etree._Element, wanted: dict, seen: set,
                     report: FillReport) -> bool:
    """Substitute text/date/number/check/choice markers in a slide's text."""
    changed = False

    def will_fill(match) -> bool:
        inner = _marker_inner(match.group(0))
        if inner is None:
            return False
        marker = marker_kind(inner.strip())
        key = slug(marker.name)
        if key not in wanted:
            return False
        if marker.kind == IMAGE:
            return False                     # can't place a picture in a run
        if marker.kind == CHECKBOX:
            return True
        return _image_bytes(wanted[key]) is None

    def resolve(marker, _full):
        key = slug(marker.name)
        if marker.kind == IMAGE or key not in wanted:
            return None
        value = wanted[key]
        if marker.kind == CHECKBOX:
            seen.add(key)
            report.filled.append(key)
            return CHECKED if _checked(value) else UNCHECKED
        if _image_bytes(value) is not None:
            return None                      # an image value can't fill text
        seen.add(key)
        report.filled.append(key)
        return _as_text(value)               # text, date, number, chosen option

    for a_p in root.iter(qn("a:p")):
        if _coalesce_markers(a_p, will_fill, _A_T, _A_R):
            changed = True
        for run in list(a_p.findall(_A_R)):
            node = run.find(_A_T)
            if node is None or node.text is None:
                continue
            filled, did = _sub_once(node.text, resolve)
            if not did:
                continue
            changed = True
            if "\n" in filled:
                _split_run_multiline(a_p, run, node, filled)
            else:
                node.text = filled
    return changed


def _split_run_multiline(a_p: etree._Element, run: etree._Element,
                         node: etree._Element, text: str) -> None:
    """Replace one run whose filled text now spans lines with a run/``a:br``/run
    sequence, each run keeping the original's properties.

    DrawingML has no in-run break the way ``w:br`` sits inside ``w:r``; a line
    break on a slide is an ``a:br`` sibling between runs, so the run is split
    rather than a break inserted inside it."""
    rpr = run.find(qn("a:rPr"))
    index = list(a_p).index(run)
    a_p.remove(run)
    for line_no, line in enumerate(_lines(text)):
        if line_no:
            a_p.insert(index, etree.Element(qn("a:br")))
            index += 1
        new_run = etree.Element(_A_R)
        if rpr is not None:
            new_run.append(deepcopy(rpr))
        text_node = etree.SubElement(new_run, _A_T)
        text_node.text = line
        a_p.insert(index, new_run)
        index += 1


def _swap_slide_pictures(pkg: OpcPackage, slide: str, root: etree._Element,
                         wanted: dict, seen: set, report: FillReport,
                         keep: bool) -> bool:
    """Swap the image inside each slide picture marked in its alt text.

    Only the blip's target changes; the picture's frame, size and position are
    the author's and stay exactly as drawn. The marker in the alt text is
    rewritten to the field's label so the finished deck carries no ``{{...}}``
    where a screen reader would read it."""
    changed = False
    for pic in root.iter(qn("p:pic")):
        inner = _picture_alt_marker(pic)
        if inner is None:
            continue
        marker = marker_kind(inner.strip())
        name = slug(marker.name) or "picture"
        blip = pic.find(".//" + qn("a:blip"))
        if blip is None:
            continue
        key = _match(wanted, seen, name)
        if key is None:
            if not keep:
                report.untouched.append(name)
            continue
        data = _image_bytes(wanted[key])
        if data is None or sniff(data) is None:
            report.untouched.append(key)     # supplied, but not embeddable
            continue
        extension, content_type = sniff(data)
        # The slide owns the relationship, but the media belongs in the deck's
        # own ppt/media, not under ppt/slides -- the PowerPoint convention.
        rid = add_image_part(pkg, data, extension, content_type, slide,
                             media_dir=posixpath.dirname(pkg.main_document))
        old_rid = blip.get(qn("r:embed"))
        blip.set(qn("r:embed"), rid)
        if old_rid and old_rid != rid:
            _release_image(pkg, slide, old_rid)
        _rewrite_picture_alt(pic, marker.label)
        report.filled.append(key)
        changed = True
    return changed


def _rewrite_picture_alt(pic: etree._Element, label: str) -> None:
    for cnv in pic.iter(qn("p:cNvPr")):
        for attr in ("descr", "title"):
            value = cnv.get(attr)
            if not value:
                continue
            new = value
            for name, pattern in _MARKERS:
                if name == "bracket":
                    continue
                new = pattern.sub(label, new)
            if new == value:
                continue
            if new.strip():
                cnv.set(attr, new)
            elif attr in cnv.attrib:
                del cnv.attrib[attr]


def _leftover(pkg: OpcPackage) -> list[str]:
    """Markers still present as literal text once filling is done."""
    found: list[str] = []
    already: set[str] = set()
    for slide in slide_parts(pkg):
        root = pkg.element(slide)
        for a_p in root.iter(qn("a:p")):
            for match in _UNAMBIGUOUS_MARKER.finditer(_paragraph_text(a_p)):
                marker = match.group(0)
                if marker not in already:
                    already.add(marker)
                    found.append(marker)
    return found
