"""Writing inferred placeholders into the donor as real content controls.

This is what makes placeholder inference survivable. `learn` will get some of
them wrong -- that is inherent to inferring a convention from examples, not a
defect to be engineered away -- so the correction path has to be something a
Word user already knows how to do. Every inferred placeholder becomes a
`w:sdt` tagged `formgen.<name>`, which shows up as a clickable control on
Word's Developer tab: a missed one is fixed by inserting a control, an
invented one by deleting it, a badly named one by editing the tag. Then
`formgen profile sync` reads all of it back.

Two rules hold the whole thing together:

* **Runs are split, never rebuilt.** A placeholder that is only part of a
  paragraph -- "Report No. LR-2024-0041" -- needs the literal and the value
  in separate runs, and the split clones the original run's `w:rPr` so the
  value keeps the formatting the author gave it.
* **Nothing is locked.** `w:sdtPr/w:lock` makes a user's in-Word edits
  bounce, which would break the correction workflow this exists to enable.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import qn

# The Word UI shows the alias; the tag is what we key on. Keeping them
# distinct means a user can rename the label without breaking the profile.
TAG_PREFIX = "formgen."


@dataclass
class MaterializeReport:
    wrapped: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def note(self) -> str | None:
        if not self.skipped:
            return None
        listed = ", ".join(f"{name} ({why})" for name, why in self.skipped[:3])
        return (
            f"{len(self.skipped)} placeholder(s) could not be made into "
            f"content controls in template.docx: {listed}"
            + (", ..." if len(self.skipped) > 3 else "")
            + ". Insert a Plain Text content control in Word, tag it "
            "formgen.<name>, and run `formgen profile sync`."
        )


def _text_nodes(paragraph: etree._Element) -> list[tuple[etree._Element, etree._Element]]:
    """(run, w:t) pairs for the paragraph's own runs, in reading order.

    Deliberately shallow: runs inside a hyperlink or a revision wrapper are
    not ours to restructure, and a field's runs must stay inside their
    `fldChar` range or the field stops being a field.
    """
    out = []
    for run in paragraph:
        if run.tag != qn("w:r"):
            continue
        for node in run:
            if node.tag == qn("w:t"):
                out.append((run, node))
    return out


def _split_run(run: etree._Element, node: etree._Element, offset: int) -> etree._Element:
    """Cut `run` in two at `offset` within `node`; return the trailing run.

    The leading half keeps the original element (so anything else pointing at
    it stays valid) and the trailing half is a clone, which is what carries
    the `w:rPr` across unchanged.
    """
    text = node.text or ""
    tail = deepcopy(run)
    for candidate in tail:
        if candidate.tag == qn("w:t"):
            candidate.text = text[offset:]
            candidate.set(qn("xml:space"), "preserve")
            break
    node.text = text[:offset]
    node.set(qn("xml:space"), "preserve")
    run.addnext(tail)
    return tail


def humanise(name: str) -> str:
    """report_number -> Report Number, for a prompt nobody configured."""
    return " ".join(word.capitalize() for word in name.split("_")) or name


def _sdt(name: str, alias: str | None = None) -> etree._Element:
    sdt = etree.Element(qn("w:sdt"))
    props = etree.SubElement(sdt, qn("w:sdtPr"))
    etree.SubElement(props, qn("w:alias")).set(qn("w:val"), alias or name)
    etree.SubElement(props, qn("w:tag")).set(qn("w:val"), TAG_PREFIX + name)
    # Deterministic id: the same corpus learned twice must produce the same
    # bytes, and Word only requires uniqueness within the document -- so a
    # hash of the name, not a counter and certainly not `hash()`, which is
    # salted per process.
    etree.SubElement(props, qn("w:id")).set(
        qn("w:val"), str(1_000_000 + (_stable_id(name) % 8_000_000)))
    etree.SubElement(props, qn("w:text"))
    etree.SubElement(sdt, qn("w:sdtContent"))
    return sdt


def _stable_id(name: str) -> int:
    from hashlib import sha1
    return int(sha1(name.encode("utf-8")).hexdigest()[:8], 16)


def wrap(paragraph: etree._Element, name: str, value: str,
         alias: str | None = None, replacement: str | None = None,
         showing: bool = True) -> str | None:
    """Wrap `value` inside `paragraph` in a content control.

    `replacement` is what the control should hold afterwards. By default it
    holds a *prompt* -- greyed text in Word reading "Report Number" -- rather
    than the value it wrapped, because the value it wrapped is real. The
    donor is a byte-faithful copy of somebody's actual report, so leaving it
    means every template ships with that person's report number, name and
    date sitting in the fields, and anyone who types over the body ships them
    onward. `overrides.yaml` can set a real default instead where the house
    format genuinely has one.

    Returns None on success, or a short reason it was left alone. Refusing is
    always better than restructuring a paragraph we do not understand: an
    un-materialized placeholder is one the user adds in Word, while a mangled
    one is a damaged template.
    """
    if not value.strip():
        return "the value is empty"
    if paragraph.find(qn("w:sdt")) is not None:
        return "already inside a content control"
    pairs = _text_nodes(paragraph)
    if not pairs:
        return "no plain runs to wrap"
    whole = "".join(node.text or "" for _, node in pairs)
    start = whole.find(value)
    if start < 0:
        return "the value is split across runs we cannot restructure"
    if whole.count(value) > 1:
        return "the value appears more than once in the paragraph"
    end = start + len(value)

    # Split the tail first: splitting the head would move the offsets the
    # tail split is measured against.
    _cut(pairs, end)
    pairs = _text_nodes(paragraph)
    _cut(pairs, start)

    runs = [run for run in paragraph if run.tag == qn("w:r")]
    offset = 0
    covered: list[etree._Element] = []
    for run in runs:
        length = sum(len(node.text or "") for node in run if node.tag == qn("w:t"))
        if offset >= start and offset + length <= end and length:
            covered.append(run)
        offset += length
    if not covered:
        return "the value did not land on a run boundary"

    sdt = _sdt(name, alias)
    covered[0].addprevious(sdt)
    content = sdt.find(qn("w:sdtContent"))
    for run in covered:
        content.append(run)
    _set_content(sdt, content,
                 replacement if replacement is not None else humanise(name),
                 showing=showing)
    return None


def _set_content(sdt: etree._Element, content: etree._Element, text: str,
                 showing: bool) -> None:
    """Replace what the control holds, keeping the formatting it had.

    `showing` sets `w:showingPlcHdr`, which is how Word knows to render the
    text greyed and to clear it the moment somebody types. Without it the
    prompt is just text, and it ends up printed in the finished report.
    """
    runs = [r for r in content if r.tag == qn("w:r")]
    properties = deepcopy(runs[0].find(qn("w:rPr"))) if runs else None
    for run in runs:
        content.remove(run)
    run = etree.Element(qn("w:r"))
    if properties is not None:
        run.append(properties)
    node = etree.SubElement(run, qn("w:t"))
    node.set(qn("xml:space"), "preserve")
    node.text = text
    content.append(run)

    props = sdt.find(qn("w:sdtPr"))
    for existing in props.findall(qn("w:showingPlcHdr")):
        props.remove(existing)
    if showing:
        # Word's schema puts showingPlcHdr before the type element.
        flag = etree.Element(qn("w:showingPlcHdr"))
        type_el = props.find(qn("w:text"))
        (type_el.addprevious(flag) if type_el is not None else props.append(flag))


def _cut(pairs: list[tuple[etree._Element, etree._Element]], offset: int) -> None:
    """Split whichever run straddles `offset`. A boundary hit is a no-op."""
    seen = 0
    for run, node in pairs:
        length = len(node.text or "")
        if seen < offset < seen + length:
            _split_run(run, node, offset - seen)
            return
        seen += length


def materialize(ctx, profile, doc: str,
                settings: dict[str, dict] | None = None) -> MaterializeReport:
    """Write every inferred placeholder into the donor's own paragraphs.

    `ctx` is the donor's :class:`DocumentContext` and `doc` its id in the
    corpus, which is how each slot finds the donor's block: the alignment
    already recorded which block each document contributed to the column.
    """
    report = MaterializeReport()
    for slot in profile.placeholders:
        index = slot.donor_index
        if index is None or index >= len(ctx.features):
            report.skipped.append((slot.name, "the donor does not have this block"))
            continue
        paragraph = ctx.features[index].block.element
        value = slot.donor_value or ctx.features[index].text
        configured = (settings or {}).get(slot.name, {})
        # `default` is a real value the house format actually fixes; `prompt`
        # is what Word greys out until somebody types. Neither configured
        # means a prompt made from the field's own name -- anything but the
        # donor's own value, which belongs to whoever wrote that report.
        default = configured.get("default")
        if default is not None:
            why = wrap(paragraph, slot.name, value,
                       replacement=str(default), showing=False)
        else:
            why = wrap(paragraph, slot.name, value,
                       replacement=configured.get("prompt"), showing=True)
        if why:
            report.skipped.append((slot.name, why))
        else:
            report.wrapped.append(slot.name)
    return report
