"""Filling a form, as opposed to generating a document.

`new` renders Markdown into a template: it keeps the cover and replaces the
body, because for a report the body is the author's and the template is only
its clothes. **A form is the other way round.** The labels, the table, the
numbered questions, the layout -- everything except the field values -- *is*
the document, and the values are the small part. Rendering Markdown over it
would throw the form away, which is precisely what `new` does to a form
today: a form has no headings, so `_detach_body` keeps nothing.

So this keeps the entire donor and changes only what goes in the fields.
That works because the profile's `template.docx` is a byte-faithful clone of
a real filled-in form -- which is also why every field the caller does *not*
supply must be cleared rather than left alone. The donor is somebody's actual
visa application; shipping it with their passport number still in it is the
worst thing this tool could do.

Three mechanisms, all of which real forms use:

* `w:sdt` content controls -- replace the content, clear the placeholder flag.
* `w:ffData` legacy form fields -- write the FORMTEXT result, tick the check
  box in `w:checked`, select the drop-down entry by index.
* visible text markers -- `###Address###`, `«Fornavn»` -- substituted in place
  inside the run that holds them.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Any

from lxml import etree

from ..learn.formfields import (
    _DOTTED, _MARKERS, CHECKBOX, CHOICE, IMAGE, in_content_control,
    marker_kind, picture_marker, slug,
)
from ..opc.ns import qn
from ..opc.package import OpcPackage
from ..oox.walk import Walker
from .images import add_image_part, build_picture_run, fit_within, native_emu
from .imageinfo import sniff

TRUTHY = {"1", "true", "yes", "y", "on", "x", "checked", "true "}

# What a filled `{{check: ...}}` marker becomes: a ballot box, ticked or not.
# A glyph rather than a Word control, because the whole point of the marker
# path is that it needs no control -- it prints and copies like any character.
CHECKED = "☒"    # ballot box with X
UNCHECKED = "☐"  # empty ballot box

# The largest an image gets when there is no slot to size it to (a typed
# `{{image: ...}}` marker, or an empty picture control): about 6 x 7.5 inches,
# so a phone photo shrinks onto the page instead of running off it.
_MAX_EMU = (5486400, 6858000)


@dataclass
class FillReport:
    filled: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)
    # Markers still sitting in the document as literal text after the fill --
    # a value that never landed, so the caller can warn instead of shipping
    # `{{...}}` into a finished document.
    leftover: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [f"{len(self.filled)} field(s) filled"]
        if self.cleared:
            bits.append(f"{len(self.cleared)} cleared")
        if self.untouched:
            bits.append(f"{len(self.untouched)} left as they were")
        return ", ".join(bits)


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else ""
    return "" if value is None else str(value)


def _checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _as_text(value).strip().lower() in TRUTHY


# -- the matcher ----------------------------------------------------------
#
# A marker is looked for over a paragraph's whole run text, not one run at a
# time, because Word rarely keeps a typed marker in a single run: an edit, a
# spell-check squiggle, or bolding half of it splits `{{ref}}` into `{{` / `ref`
# / `}}`, each its own run. `_coalesce_markers` pulls such a split marker back
# into one run before it is filled; `_sub_once` then does the substitution in a
# single left-to-right pass so a value that itself looks like a marker is never
# re-scanned.

# Every marker style in one alternation, tried in the order they are declared;
# their delimiters do not overlap, so at any position at most one style begins.
_ANY_MARKER = re.compile("|".join(f"(?:{pat.pattern})" for _, pat in _MARKERS))

# The same, minus the `[Name]` bracket style: its delimiters are ordinary
# punctuation, so `[see appendix]` would read as a marker. Used only to warn
# about markers left unfilled, where a false alarm on plain prose is worse than
# missing a bracket marker nobody types by hand anyway.
_UNAMBIGUOUS_MARKER = re.compile("|".join(
    f"(?:{pat.pattern})" for name, pat in _MARKERS if name != "bracket"))


def _marker_inner(full: str) -> str | None:
    """The inner text of one whole marker (e.g. `image: photo` from
    `{{image: photo}}`), by matching it against each style's own pattern."""
    for _, pattern in _MARKERS:
        hit = pattern.fullmatch(full)
        if hit is not None:
            return hit.group(1)
    return None


def _text_segments(paragraph: etree._Element):
    """The paragraph's run text concatenated, with a map from each `w:t` node to
    its span in that string. Only text that sits directly in a run counts --
    which is exactly the text a marker can be typed into."""
    parts: list[str] = []
    segments: list[tuple[etree._Element, int, int]] = []
    pos = 0
    for node in paragraph.iter(qn("w:t")):
        parent = node.getparent()
        if parent is None or parent.tag != qn("w:r"):
            continue
        text = node.text or ""
        segments.append((node, pos, pos + len(text)))
        parts.append(text)
        pos += len(text)
    return "".join(parts), segments


def _first_split(text: str, segments: list, should_pull) -> tuple[int, int] | None:
    """The span of the first marker that crosses a run boundary and that
    `should_pull` wants coalesced; None if there is no such marker."""
    for match in _ANY_MARKER.finditer(text):
        crossed = [s for s in segments if s[1] < match.end() and s[2] > match.start()]
        if len(crossed) > 1 and should_pull(match):
            return match.start(), match.end()
    return None


def _pull_into_first(segments: list, start: int, end: int, text: str) -> None:
    """Move a marker spanning several `w:t` nodes into the first of them.

    The paragraph's visible text is unchanged: the first node keeps its text up
    to the marker and gains the whole marker, the nodes fully inside the marker
    are emptied, and the last keeps its text after the marker. The value is then
    written by the ordinary single-run path, taking the first run's formatting.
    """
    crossed = [s for s in segments if s[1] < end and s[2] > start]
    first_node, first_start, _ = crossed[0]
    last_node, last_start, _ = crossed[-1]
    head = (first_node.text or "")[: start - first_start]
    tail = (last_node.text or "")[end - last_start:]
    first_node.text = head + text[start:end]
    first_node.set(qn("xml:space"), "preserve")
    for middle_node, _, _ in crossed[1:-1]:
        middle_node.text = ""
    last_node.text = tail


def _coalesce_markers(paragraph: etree._Element, should_pull) -> bool:
    """Pull every fillable split marker in the paragraph into a single run.

    `should_pull(match)` decides marker by marker -- only markers about to be
    filled are moved, so a split marker with no value stays byte-for-byte as the
    author left it. Returns whether anything moved."""
    changed = False
    while True:
        text, segments = _text_segments(paragraph)
        span = _first_split(text, segments, should_pull)
        if span is None:
            return changed
        _pull_into_first(segments, span[0], span[1], text)
        changed = True


def _sub_once(text: str, resolve) -> tuple[str, bool]:
    """Substitute every marker in `text` in one left-to-right pass.

    `resolve((kind, name, choices), whole_marker)` returns the replacement, or
    None to leave the marker untouched. Because the scan is over the original
    text and replacements are appended, a value that happens to look like a
    marker is emitted verbatim, never treated as one."""
    out: list[str] = []
    last = 0
    changed = False
    for match in _ANY_MARKER.finditer(text):
        inner = _marker_inner(match.group(0))
        if inner is None:
            continue
        replacement = resolve(marker_kind(inner.strip()), match.group(0))
        if replacement is None:
            continue
        out.append(text[last:match.start()])
        out.append(replacement)
        last = match.end()
        changed = True
    if not changed:
        return text, False
    out.append(text[last:])
    return "".join(out), True


def fill(pkg: OpcPackage, values: dict[str, Any],
         keep_unsupplied: bool = False) -> FillReport:
    """Write `values` into every field of `pkg`, in place.

    The fields are located and named by `find_fields`, not by re-deriving the
    names here: a form's fields are named from the labels printed next to
    them, and two implementations of that would agree right up until the
    document that matters.

    `keep_unsupplied` leaves a field the caller said nothing about holding
    whatever the donor held. It defaults to False and should stay that way:
    the donor is a real document and its values are somebody's.
    """
    from ..learn.formfields import find_fields

    report = FillReport()
    wanted = {slug(k): v for k, v in values.items()}
    seen: set[str] = set()

    # Repeating rows first: this clones template rows into real ones, so field
    # discovery and the block walk below both see the document's final row
    # layout -- otherwise a legacy field in a row after the repeat is keyed to
    # a path that no longer exists once the rows multiply.
    _fill_repeats(pkg, wanted, seen, report)

    by_block: dict[str, list] = {}
    for item in find_fields(pkg).fields:
        by_block.setdefault(item.block_path, []).append(item)

    # One counter for every drawing this fill adds -- markers and picture
    # controls alike -- so no two images end up sharing a wp:docPr id.
    root = pkg.element(pkg.main_document)
    drawing_ids = [_max_drawing_id(root)]

    # Every part whose tree we edit has to be marked dirty, or save() writes it
    # back byte-for-byte and the edit vanishes. This is how a value filled into
    # a header or footer actually reaches the file -- and how a donor's value
    # in one gets cleared rather than silently shipped.
    edited: set[str] = set()
    for block in Walker(pkg).blocks():
        if not block.is_paragraph:
            continue
        here = by_block.get(block.path, [])
        part = block.context.part
        _fill_ffdata(block.element, here, wanted, seen, report,
                     keep_unsupplied, part, edited)
        _fill_markers(block.element, wanted, seen, report, pkg, drawing_ids,
                      part, edited)
    _fill_controls(pkg, wanted, seen, report, keep_unsupplied, drawing_ids)
    _fill_marked_pictures(pkg, wanted, seen, report, keep_unsupplied)
    for part in edited:
        pkg.touch(part)

    # Untouched parts are written back byte for byte -- that preservation
    # guarantee is the point of the OPC layer, and it means an edited tree
    # that is never marked dirty is silently discarded on save.
    pkg.touch(pkg.main_document)
    report.unknown = sorted(set(wanted) - seen)
    report.leftover = _leftover_markers(pkg)
    return report


def _leftover_markers(pkg: OpcPackage) -> list[str]:
    """Markers still present as literal text once filling is done.

    Scanned over each paragraph's whole run text, so a marker split across runs
    counts too. Distinct, in reading order -- the point is to tell the caller a
    value never landed, not to enumerate every occurrence."""
    found: list[str] = []
    already: set[str] = set()
    for block in Walker(pkg).blocks():
        if not block.is_paragraph:
            continue
        text, _ = _text_segments(block.element)
        for match in _UNAMBIGUOUS_MARKER.finditer(text):
            marker = match.group(0)
            if marker not in already:
                already.add(marker)
                found.append(marker)
    return found


# -- legacy form fields ---------------------------------------------------


def _fill_ffdata(paragraph: etree._Element, here: list, wanted: dict,
                 seen: set, report: FillReport, keep: bool,
                 part: str, edited: set) -> None:
    from ..learn.formfields import _ff_kind

    runs = [r for r in paragraph.iter(qn("w:r"))]
    boxes = [d for d in paragraph.iter(qn("w:ffData"))]
    if not boxes:
        return
    named = {f.ordinal: f for f in here if f.source == "ffData"}

    for index, data in enumerate(boxes):
        kind, choices, _ = _ff_kind(data)
        item = named.get(index)
        name = item.name if item else ""
        key = _match(wanted, seen, name)
        if key is None:
            if keep or kind == CHECKBOX:
                report.untouched.append(name or "field")
            elif _write_result(runs, data, ""):
                report.cleared.append(name or "field")
                edited.add(part)
            continue
        value = wanted[key]
        if kind == CHECKBOX:
            _set_checked(data, _checked(value))
        elif kind == CHOICE:
            _set_choice(data, choices, _as_text(value))
            _write_result(runs, data, _as_text(value))
        else:
            _write_result(runs, data, _as_text(value))
        report.filled.append(key)
        edited.add(part)


def _match(wanted: dict, seen: set, *candidates: str) -> str | None:
    for candidate in candidates:
        if candidate and candidate in wanted:
            seen.add(candidate)
            return candidate
    return None


def _set_checked(data: etree._Element, on: bool) -> None:
    box = data.find(qn("w:checkBox"))
    if box is None:
        return
    for existing in box.findall(qn("w:checked")):
        box.remove(existing)
    element = etree.SubElement(box, qn("w:checked"))
    if not on:
        element.set(qn("w:val"), "0")


def _set_choice(data: etree._Element, choices: tuple[str, ...], value: str) -> None:
    ddlist = data.find(qn("w:ddList"))
    if ddlist is None:
        return
    try:
        index = [c.strip().lower() for c in choices].index(value.strip().lower())
    except ValueError:
        return
    for existing in ddlist.findall(qn("w:result")):
        ddlist.remove(existing)
    ddlist.insert(0, etree.Element(qn("w:result")))
    ddlist[0].set(qn("w:val"), str(index))


def _write_result(runs: list, data: etree._Element, value: str) -> bool:
    """Replace the text between this field's `separate` and its `end`.

    A field's *result* is what the reader sees; the instruction is not. Runs
    outside the separate..end window belong to the field's machinery and
    rewriting them would stop it being a field.
    """
    begin = data.getparent()
    while begin is not None and begin.tag != qn("w:r"):
        begin = begin.getparent()
    if begin is None or begin not in runs:
        return False
    start = runs.index(begin)
    depth, separate, end = 0, None, None
    for run in runs[start:]:
        for child in run:
            if child.tag != qn("w:fldChar"):
                continue
            kind = child.get(qn("w:fldCharType"))
            if kind == "begin":
                depth += 1
            elif kind == "separate" and depth == 1 and separate is None:
                separate = run
            elif kind == "end":
                depth -= 1
                if depth == 0:
                    end = run
                    break
        if end is not None:
            break
    if separate is None or end is None:
        return False

    window = runs[runs.index(separate) + 1:runs.index(end)]
    if not window:
        return False
    for run in window[1:]:
        for node in run.findall(qn("w:t")):
            run.remove(node)
    first = window[0]
    for node in first.findall(qn("w:t")):
        first.remove(node)
    # Word gives an empty field a run of en-spaces so it stays clickable.
    node = etree.SubElement(first, qn("w:t"))
    node.set(qn("xml:space"), "preserve")
    node.text = value or "     "
    return True


# -- content controls -----------------------------------------------------


def _fill_controls(pkg: OpcPackage, wanted: dict, seen: set,
                   report: FillReport, keep: bool, drawing_ids: list) -> None:
    root = pkg.element(pkg.main_document)
    next_drawing_id = drawing_ids[0]
    for sdt in root.iter(qn("w:sdt")):
        props = sdt.find(qn("w:sdtPr"))
        content = sdt.find(qn("w:sdtContent"))
        if props is None or content is None:
            continue
        tag = props.find(qn("w:tag"))
        name = (tag.get(qn("w:val")) if tag is not None else "") or ""
        if name.startswith("formgen."):
            name = name[len("formgen."):]
        if not name:
            alias = props.find(qn("w:alias"))
            name = (alias.get(qn("w:val")) if alias is not None else "") or ""
        key = _match(wanted, seen, slug(name))

        if props.find(qn("w:picture")) is not None:
            next_drawing_id = _fill_picture(
                pkg, props, content, key, wanted, report,
                slug(name) or "picture", next_drawing_id)
            continue

        if key is None:
            if keep:
                report.untouched.append(slug(name) or "control")
                continue
            value, record = "", report.cleared
        else:
            value, record = _as_text(wanted[key]), report.filled
        for flag in props.findall(qn("w:showingPlcHdr")):
            props.remove(flag)
        _write_into(content, value)
        record.append(key or slug(name) or "control")


# -- picture content controls ---------------------------------------------


def _image_bytes(value: Any) -> bytes | None:
    """The image behind a supplied value, or None if it is not an image.

    A picture slot is filled with bytes or a path to them. A plain string is
    text, not a filename, so it fills nothing here -- the field is skipped
    rather than having its own name written into it as a caption.
    """
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, PathLike):
        try:
            return Path(value).read_bytes()
        except OSError:
            return None
    return None


def _fill_picture(pkg: OpcPackage, props: etree._Element,
                  content: etree._Element, key: str | None, wanted: dict,
                  report: FillReport, name: str, next_drawing_id: int) -> int:
    """Put an uploaded image into a picture control, in place.

    The common case swaps the target of the blip already in the control, so
    the drawing the author sized and positioned is untouched but for its
    `r:embed` and extent. An empty control has no blip, so a fresh drawing is
    built. Runs are never rebuilt; a picture slot with nothing supplied is
    left alone, because there is no safe empty image to clear it to.
    """
    if key is None:
        report.untouched.append(name)
        return next_drawing_id
    data = _image_bytes(wanted[key])
    if data is None:
        report.untouched.append(name)  # supplied, but not an image
        return next_drawing_id
    fmt = sniff(data)
    if fmt is None:
        report.untouched.append(name)  # not a format Word embeds
        return next_drawing_id

    extension, content_type = fmt
    rid = add_image_part(pkg, data, extension, content_type)
    for flag in props.findall(qn("w:showingPlcHdr")):
        props.remove(flag)

    native = native_emu(data)
    blip = content.find(".//" + qn("a:blip"))
    if blip is not None:
        box = _extent_of(content)
        width, height = fit_within(native, box) if (native and box) else \
            (box or native or (0, 0))
        old_rid = blip.get(qn("r:embed"))
        blip.set(qn("r:embed"), rid)
        if width and height:
            _set_extent(content, width, height)
        # The image the control used to hold is now referenced by nothing.
        # Left in place it is dead weight -- and, when the template was a real
        # filled document, somebody's photo still sitting in the file. Drop it.
        if old_rid and old_rid != rid:
            _release_image(pkg, pkg.main_document, old_rid)
    else:
        # No drawing means no author-drawn box, so there is nothing to fit to;
        # use the image's own size, only shrinking it if it would overflow a
        # page, so a phone photo does not land six feet wide.
        width, height = native or (2743200, 2057400)  # ~3x2.25in fallback
        if width > _MAX_EMU[0] or height > _MAX_EMU[1]:
            width, height = fit_within((width, height), _MAX_EMU)
        host = content.find(qn("w:p"))
        target = host if host is not None else content
        for placeholder in target.findall(qn("w:r")):
            target.remove(placeholder)  # drop the "click to add" prompt run
        target.append(build_picture_run(rid, width, height,
                                        next_drawing_id + 1,
                                        name.title() or "Picture"))
        next_drawing_id += 1
    report.filled.append(key)
    return next_drawing_id


def _fill_marked_pictures(pkg: OpcPackage, wanted: dict, seen: set,
                          report: FillReport, keep: bool) -> None:
    """Swap the image inside each picture an author marked in its alt text.

    Unlike a text ``{{image: ...}}`` marker, the picture is already placed --
    with the size, position, wrap and borders the author gave it. So nothing is
    rebuilt: only the blip's target changes, and the drawing stays exactly as it
    was. The marker in the alt text is replaced by the field's label (or cleared)
    so the finished document does not carry ``{{...}}`` where a screen reader
    would read it."""
    root = pkg.element(pkg.main_document)
    for drawing in root.iter(qn("w:drawing")):
        if in_content_control(drawing):
            continue
        inner = picture_marker(drawing)
        if inner is None:
            continue
        marker = marker_kind(inner.strip())
        name = slug(marker.name) or "picture"
        blip = drawing.find(".//" + qn("a:blip"))
        if blip is None:
            continue  # a marked shape with no image to swap
        key = _match(wanted, seen, name)
        if key is None:
            if not keep:
                report.untouched.append(name)
            continue
        data = _image_bytes(wanted[key])
        if data is None or sniff(data) is None:
            report.untouched.append(key)  # supplied, but not an image Word embeds
            continue
        extension, content_type = sniff(data)
        rid = add_image_part(pkg, data, extension, content_type)
        old_rid = blip.get(qn("r:embed"))
        blip.set(qn("r:embed"), rid)
        if old_rid and old_rid != rid:
            _release_image(pkg, pkg.main_document, old_rid)
        _rewrite_picture_alt(drawing, marker.label)
        report.filled.append(key)


def _rewrite_picture_alt(drawing: etree._Element, label: str) -> None:
    """Replace the marker in a drawing's alt text with the field's label (or
    remove the attribute when nothing readable is left)."""
    def rewrite(el: etree._Element, attr: str) -> None:
        value = el.get(attr)
        if not value:
            return
        new = value
        for name, pattern in _MARKERS:
            if name == "bracket":
                continue
            new = pattern.sub(label, new)
        if new == value:
            return
        if new.strip():
            el.set(attr, new)
        elif attr in el.attrib:
            del el.attrib[attr]

    for doc_pr in drawing.iter(qn("wp:docPr")):
        rewrite(doc_pr, "descr")
        rewrite(doc_pr, "title")
    for cnv in drawing.iter(qn("pic:cNvPr")):
        rewrite(cnv, "descr")


def _release_image(pkg: OpcPackage, owner: str, old_rid: str) -> None:
    """Drop a picture's now-unreferenced image part and its relationship.

    Only when nothing else points at it: another blip may still use the same
    `r:embed`, and two rels may share one media part. So this bails if any blip
    still references the id, and keeps the part if any surviving relationship
    on the owner still resolves to it -- dropping only what is truly orphaned.
    """
    root = pkg.element(owner)
    if any(b.get(qn("r:embed")) == old_rid for b in root.iter(qn("a:blip"))):
        return
    rels = pkg.rels(owner)
    rel = rels.get(old_rid)
    if rel is None or rel.external:
        return
    target = rel.resolve(owner)
    part = pkg.actual_name(target) if target else None
    pkg.touch_rels(owner).drop(old_rid)
    if part is None:
        return
    for other in pkg.rels(owner):
        resolved = other.resolve(owner)
        if resolved and pkg.actual_name(resolved) == part:
            return  # still reachable by another relationship
    pkg.drop_part(part)


def _max_drawing_id(root: etree._Element) -> int:
    """The largest `wp:docPr@id` in the document; new drawings go above it,
    since two drawings sharing an id makes Word renumber them on open."""
    top = 0
    for doc_pr in root.iter(qn("wp:docPr")):
        try:
            top = max(top, int(doc_pr.get("id", "0")))
        except ValueError:
            continue
    return top


def _extent_of(content: etree._Element) -> tuple[int, int] | None:
    extent = content.find(".//" + qn("wp:extent"))
    if extent is None:
        return None
    try:
        return int(extent.get("cx", "0")), int(extent.get("cy", "0"))
    except ValueError:
        return None


def _set_extent(content: etree._Element, width: int, height: int) -> None:
    """Resize the drawing to the fitted image: the frame's `wp:extent` and the
    picture's own `a:ext`, which Word keeps in step."""
    extent = content.find(".//" + qn("wp:extent"))
    if extent is not None:
        extent.set("cx", str(width))
        extent.set("cy", str(height))
    ext = content.find(".//" + qn("a:ext"))
    if ext is not None:
        ext.set("cx", str(width))
        ext.set("cy", str(height))


def _write_into(content: etree._Element, value: str) -> None:
    from copy import deepcopy

    host = content.find(qn("w:p"))
    if host is None:
        host = content
    template = host.find(qn("w:r"))
    properties = deepcopy(template.find(qn("w:rPr"))) if template is not None else None
    for run in host.findall(qn("w:r")):
        host.remove(run)
    run = etree.Element(qn("w:r"))
    if properties is not None:
        run.append(properties)
    _set_run_text(run, value)
    host.append(run)


def _lines(value: str) -> list[str]:
    """Split on line breaks, treating CRLF and CR as one break -- a browser
    textarea sends CRLF, and a stray CR left in a w:t prints as a box."""
    return value.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _set_run_text(run: etree._Element, value: str) -> None:
    """Fill `run` with `value` as w:t nodes, a `w:br` at every newline.

    A single-line value is one `w:t`, byte-for-byte what a plain assignment
    gave before; a multi-line one keeps its breaks instead of collapsing onto
    a single line, which is what Word does with a raw newline in a `w:t`.
    """
    for index, line in enumerate(_lines(value)):
        if index:
            etree.SubElement(run, qn("w:br"))
        node = etree.SubElement(run, qn("w:t"))
        node.set(qn("xml:space"), "preserve")
        node.text = line


# -- visible markers ------------------------------------------------------


def _fill_markers(paragraph: etree._Element, wanted: dict, seen: set,
                  report: FillReport, pkg: OpcPackage, drawing_ids: list,
                  part: str, edited: set) -> None:
    """Fill the typed markers in a paragraph.

    Text markers -- ``{{name}}``, ``«name»``, ``###name###`` -- are substituted
    inside the run that holds them, so the marker keeps whatever formatting the
    author gave it. An image marker -- ``{{image: name}}`` -- can't be text, so
    its run is split and a picture is dropped in where the marker sat. A marker
    Word has split across runs is first pulled back into one run, so it fills
    like any other instead of being silently missed.
    """
    def _will_fill(match):
        inner = _marker_inner(match.group(0))
        if inner is None:
            return False
        marker = marker_kind(inner.strip())
        key = slug(marker.name)
        if key not in wanted:
            return False
        value = wanted[key]
        if marker.kind == IMAGE:
            data = _image_bytes(value)
            return data is not None and sniff(data) is not None
        if marker.kind == CHECKBOX:
            return True
        return _image_bytes(value) is None  # an image value can't fill text

    if _coalesce_markers(paragraph, _will_fill):
        edited.add(part)

    _replace_image_markers(paragraph, wanted, seen, report, pkg, drawing_ids,
                           part, edited)

    def _resolve(marker, _full):
        key = slug(marker.name)
        # Image markers are handled above; a supplied image value can never
        # fill a text-shaped marker.
        if marker.kind == IMAGE or key not in wanted:
            return None
        value = wanted[key]
        if marker.kind == CHECKBOX:
            seen.add(key)
            report.filled.append(key)
            return CHECKED if _checked(value) else UNCHECKED
        if _image_bytes(value) is not None:
            return None
        seen.add(key)
        report.filled.append(key)
        return _as_text(value)  # text, date, number, or the chosen option

    for node in paragraph.iter(qn("w:t")):
        text = node.text or ""
        if not text:
            continue
        filled, changed = _sub_once(text, _resolve)
        if not changed:
            continue
        edited.add(part)
        if "\n" in filled:
            _rewrite_multiline(node, filled)
        else:
            node.text = filled
            node.set(qn("xml:space"), "preserve")


def _replace_image_markers(paragraph: etree._Element, wanted: dict, seen: set,
                           report: FillReport, pkg: OpcPackage,
                           drawing_ids: list, part: str, edited: set) -> None:
    """Turn each ``{{image: name}}`` whose image was supplied into a picture.

    The run holding the marker is split into the text before it, a run bearing
    the image, and the text after -- so a marker mid-sentence keeps its
    neighbours. One image per run is handled; a second marker in the same run
    is caught on the next pass Word gives us, which is plenty for real forms.
    """
    for run in list(paragraph.findall(qn("w:r"))):
        node = run.find(qn("w:t"))
        if node is None or not node.text:
            continue
        for _, pattern in _MARKERS:
            match = pattern.search(node.text)
            while match is not None:
                marker = marker_kind(match.group(1).strip())
                key = slug(marker.name)
                data = _image_bytes(wanted[key]) if key in wanted else None
                # Only claim the field if the picture actually went in: an
                # upload Word cannot embed (WebP, SVG, HEIC) leaves the marker
                # untouched, and must not be reported as filled. The author's
                # `| label` becomes the picture's alt text.
                if marker.kind == IMAGE and data is not None and _split_run_around_image(
                        paragraph, run, node, match.start(), match.end(),
                        data, marker.name, drawing_ids, pkg, part, marker.label):
                    seen.add(key)
                    report.filled.append(key)
                    edited.add(part)
                    break  # this run is gone; move to the next
                match = pattern.search(node.text, match.end())
            else:
                continue
            break


def _split_run_around_image(paragraph: etree._Element, run: etree._Element,
                            node: etree._Element, start: int, end: int,
                            data: bytes, name: str, drawing_ids: list,
                            pkg: OpcPackage, owner: str, alt: str = "") -> bool:
    fmt = sniff(data)
    if fmt is None:
        return False
    extension, content_type = fmt
    rid = add_image_part(pkg, data, extension, content_type, owner)
    width, height = native_emu(data) or (2743200, 2057400)
    if width > _MAX_EMU[0] or height > _MAX_EMU[1]:
        width, height = fit_within((width, height), _MAX_EMU)
    drawing_ids[0] += 1
    # The alt text (the author's label) is what a screen reader announces and
    # what Word shows under Format Picture > Alt Text -- a real accessibility
    # element a bare text marker would otherwise drop.
    picture = build_picture_run(rid, width, height, drawing_ids[0],
                                alt or name.title() or "Picture", alt)

    text = node.text
    before, after = text[:start], text[end:]
    rpr = run.find(qn("w:rPr"))
    index = paragraph.index(run)
    paragraph.remove(run)
    for element in _run_sequence(before, picture, after, rpr):
        paragraph.insert(index, element)
        index += 1
    return True


def _run_sequence(before: str, picture: etree._Element, after: str,
                  rpr: etree._Element | None):
    """before-text run, the picture run, after-text run -- skipping the empty
    text runs, and giving each text run a copy of the marker's formatting."""
    from copy import deepcopy

    if before:
        yield _text_run(before, deepcopy(rpr) if rpr is not None else None)
    yield picture
    if after:
        yield _text_run(after, deepcopy(rpr) if rpr is not None else None)


# -- repeating rows -------------------------------------------------------


def _fill_repeats(pkg: OpcPackage, wanted: dict, seen: set,
                  report: FillReport) -> None:
    """Clone a repeating unit once per record supplied for its collection.

    A dotted marker (`{{items.qty}}`) marks a repeating column; the unit that
    repeats is the table row it sits in, or the paragraph if it is not in a
    table. Each record in ``wanted[collection]`` becomes one clone with its own
    values; the template unit is then removed. Runs inside a clone are edited,
    never rebuilt, so the row keeps its borders, shading and fonts.
    """
    root = pkg.element(pkg.main_document)

    # A dotted marker can be split across runs too; pull each one whose
    # collection was supplied into a single run before it is detected and
    # cloned, so a broken-up `{{items.qty}}` is not silently skipped.
    def _will_repeat(match):
        inner = _marker_inner(match.group(0))
        if inner is None:
            return False
        dotted = _DOTTED.match(marker_kind(inner.strip()).name)
        return dotted is not None and isinstance(
            wanted.get(slug(dotted.group(1))), list)

    for paragraph in root.iter(qn("w:p")):
        _coalesce_markers(paragraph, _will_repeat)

    units: dict[str, etree._Element] = {}
    for node in root.iter(qn("w:t")):
        text = node.text or ""
        if "." not in text:
            continue
        for _, pattern in _MARKERS:
            for match in pattern.finditer(text):
                dotted = _DOTTED.match(marker_kind(match.group(1).strip()).name)
                if dotted is None:
                    continue
                collection = slug(dotted.group(1))
                if collection not in units:
                    unit = _repeat_unit(node)
                    if unit is not None:
                        units[collection] = unit

    # Images cloned into rows need drawing ids above every one already in the
    # document; the block walk that runs after this recomputes its own base
    # from the tree, so it sees the rows' drawings and never collides.
    drawing_ids = [_max_drawing_id(root)]
    for collection, unit in units.items():
        records = wanted.get(collection)
        if not isinstance(records, list):
            continue
        seen.add(collection)
        parent = unit.getparent()
        if parent is None:
            continue
        index = parent.index(unit)
        parent.remove(unit)
        for record in records:
            if not isinstance(record, dict):
                continue
            clone = deepcopy(unit)
            _fill_dotted(clone, collection, record, pkg, drawing_ids)
            parent.insert(index, clone)
            index += 1
        report.filled.append(collection)


def _repeat_unit(node: etree._Element) -> etree._Element | None:
    """The nearest table row above `node`, or failing that its paragraph."""
    row = para = None
    element: etree._Element | None = node
    while element is not None:
        if element.tag == qn("w:tr") and row is None:
            row = element
        elif element.tag == qn("w:p") and para is None:
            para = element
        element = element.getparent()
    return row if row is not None else para


def _fill_dotted(clone: etree._Element, collection: str, record: dict,
                 pkg: OpcPackage, drawing_ids: list) -> None:
    """Fill one cloned row from one record.

    A column is whatever kind its marker is: a text column is substituted, a
    tick-box column becomes a ballot glyph, a choice column the chosen option,
    and an image column has its run split and a picture dropped in -- exactly as
    a single field of each kind would, but keyed by the part after the dot."""
    values = {slug(k): v for k, v in record.items()}

    def _column(name: str) -> str | None:
        dotted = _DOTTED.match(name)
        if dotted is None or slug(dotted.group(1)) != collection:
            return None
        return slug(dotted.group(2).strip())

    def _resolve(marker, _full):
        column = _column(marker.name)
        if column is None:
            return None
        if marker.kind == IMAGE:
            # Leave a supplied image for the picture pass below; clear the
            # marker for a row that has none, so no `{{image: ...}}` ships.
            return None if _image_bytes(values.get(column)) is not None else ""
        if marker.kind == CHECKBOX:
            return CHECKED if _checked(values.get(column)) else UNCHECKED
        return _as_text(values.get(column, ""))  # text/date/number/chosen option

    for node in clone.iter(qn("w:t")):
        text = node.text or ""
        if "." not in text:
            continue
        filled, changed = _sub_once(text, _resolve)
        if not changed:
            continue
        if "\n" in filled:
            _rewrite_multiline(node, filled)
        else:
            node.text = filled
            node.set(qn("xml:space"), "preserve")

    _place_dotted_images(clone, collection, values, pkg, drawing_ids)


def _place_dotted_images(clone: etree._Element, collection: str, values: dict,
                         pkg: OpcPackage, drawing_ids: list) -> None:
    """Drop each row's `{{image: items.photo}}` picture into the clone.

    Mirrors `_replace_image_markers`, but the bytes come from the record's
    column rather than a top-level value, and there is no report to write --
    the whole collection is reported filled once by `_fill_repeats`."""
    for paragraph in clone.iter(qn("w:p")):
        for run in list(paragraph.findall(qn("w:r"))):
            node = run.find(qn("w:t"))
            if node is None or not node.text:
                continue
            for _, pattern in _MARKERS:
                match = pattern.search(node.text)
                while match is not None:
                    marker = marker_kind(match.group(1).strip())
                    dotted = _DOTTED.match(marker.name) if marker.kind == IMAGE else None
                    data = None
                    if dotted is not None and slug(dotted.group(1)) == collection:
                        data = _image_bytes(values.get(slug(dotted.group(2).strip())))
                    if data is not None and _split_run_around_image(
                            paragraph, run, node, match.start(), match.end(),
                            data, dotted.group(2), drawing_ids, pkg,
                            pkg.main_document, marker.label):
                        break  # this run is gone; move to the next
                    match = pattern.search(node.text, match.end())
                else:
                    continue
                break


def _rewrite_multiline(node: etree._Element, text: str) -> None:
    """Replace a single w:t (whose substituted text now spans lines) with the
    w:t/w:br sequence that renders those lines, keeping the run's order."""
    run = node.getparent()
    index = list(run).index(node)
    run.remove(node)
    for line_no, line in enumerate(_lines(text)):
        if line_no:
            run.insert(index, etree.Element(qn("w:br")))
            index += 1
        piece = etree.Element(qn("w:t"))
        piece.set(qn("xml:space"), "preserve")
        piece.text = line
        run.insert(index, piece)
        index += 1


def _text_run(text: str, rpr: etree._Element | None) -> etree._Element:
    run = etree.Element(qn("w:r"))
    if rpr is not None:
        run.append(rpr)
    _set_run_text(run, text)
    return run
