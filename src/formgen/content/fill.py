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

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from ..learn.formfields import _MARKERS, CHECKBOX, CHOICE, slug
from ..opc.ns import qn
from ..opc.package import OpcPackage
from ..oox.walk import Walker

TRUTHY = {"1", "true", "yes", "y", "on", "x", "checked", "true "}


@dataclass
class FillReport:
    filled: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)

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

    by_block: dict[str, list] = {}
    for item in find_fields(pkg).fields:
        by_block.setdefault(item.block_path, []).append(item)

    for block in Walker(pkg).blocks():
        if not block.is_paragraph:
            continue
        here = by_block.get(block.path, [])
        _fill_ffdata(block.element, here, wanted, seen, report, keep_unsupplied)
        _fill_markers(block.element, here, wanted, seen, report)
    _fill_controls(pkg, wanted, seen, report, keep_unsupplied)

    # Untouched parts are written back byte for byte -- that preservation
    # guarantee is the point of the OPC layer, and it means an edited tree
    # that is never marked dirty is silently discarded on save.
    pkg.touch(pkg.main_document)
    report.unknown = sorted(set(wanted) - seen)
    return report


# -- legacy form fields ---------------------------------------------------


def _fill_ffdata(paragraph: etree._Element, here: list, wanted: dict,
                 seen: set, report: FillReport, keep: bool) -> None:
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
                   report: FillReport, keep: bool) -> None:
    root = pkg.element(pkg.main_document)
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
    node = etree.SubElement(run, qn("w:t"))
    node.set(qn("xml:space"), "preserve")
    node.text = value
    host.append(run)


# -- visible markers ------------------------------------------------------


def _fill_markers(paragraph: etree._Element, here: list, wanted: dict,
                  seen: set, report: FillReport) -> None:
    """Substitute ###Address### and friends inside the run that holds them.

    Runs are edited, never rebuilt, so the marker keeps whatever formatting
    the form gave it -- which on a real form is often the underline that
    makes it look like a blank to write on.
    """
    for node in paragraph.iter(qn("w:t")):
        text = node.text or ""
        if not text:
            continue
        for _, pattern in _MARKERS:
            def _substitute(match, _node=node):
                key = slug(match.group(1))
                if key in wanted:
                    seen.add(key)
                    report.filled.append(key)
                    return _as_text(wanted[key])
                return match.group(0)
            text = pattern.sub(_substitute, text)
        if text != (node.text or ""):
            node.text = text
            node.set(qn("xml:space"), "preserve")
