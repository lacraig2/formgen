"""Fields a single document declares about itself.

Corpus alignment finds a placeholder by noticing that twelve reports differ
in the same place. A **form** does not need that: it says where its fields
are, in its own markup, and one document is enough. That matters because
forms and structured-but-hand-made documents are the common case, and a
corpus of twelve filled-in copies of a form is exactly what nobody has.

Four declarations, in descending order of how much they can be trusted:

1. **`w:sdt` content controls.** Modern, named, and what `formgen` itself
   writes.
2. **`w:ffData` legacy form fields** -- Word's Forms toolbar, text inputs,
   check boxes and drop-downs. Structurally certain about *where* the field
   is and almost useless about what it is called: Word names them `Text42`
   and `Check1`, so the name has to come from the label beside them.
3. **Field instructions** -- `MERGEFIELD`, `FILLIN`, `ASK`, `DOCPROPERTY`,
   `DOCVARIABLE`. Authoritative names, because a human typed them.
4. **Visible text markers** -- `###Address###`, `«Fornavn»`, `[Job Title]`,
   a rule of underscores. A convention rather than a mechanism, so these are
   offered with low confidence and marked for review.

Labels are found the way a reader finds them, which on a real form is not
"the text just before": it is the cell above, or the words after the check
box, or the line above inside the same cell. All three are here because all
three occur on the first real form we tried this against.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, NamedTuple

from lxml import etree

from ..opc.ns import qn
from ..opc.package import OpcPackage
from ..oox.walk import Block, Walker, field_instructions

TEXT = "text"
CHECKBOX = "checkbox"
CHOICE = "choice"
IMAGE = "image"
DATE = "date"
NUMBER = "number"

# Word's own auto-generated names carry no meaning -- Text42 is the
# forty-second field somebody dropped in, not a description of anything.
_AUTO_NAME = re.compile(r"^(?:text|check|dropdown|dropdown list|list)\s*\d*$", re.I)

# "01 -  Full name", "3. Date of birth", "12) Sex" -- form questions are
# numbered, and the number is not part of the name.
_QUESTION_NUMBER = re.compile(
    r"^\s*\(?\d{1,3}\)?\s*[-‐-―.:)]\s*(?=\S)")

# Word fills an empty form field with en-spaces so it has a clickable width.
_FIELD_FILLER = re.compile(r"^[\s -​ ._]*$")

_INSTRUCTION = re.compile(
    r"^\s*(MERGEFIELD|FILLIN|ASK|DOCPROPERTY|DOCVARIABLE)\s+"
    r'"?([A-Za-z0-9_ .\-]+?)"?\s*(?:\\|$)', re.I)

_MARKERS = (
    ("###", re.compile(r"###\s*([^#]{1,40}?)\s*###")),
    ("merge", re.compile(r"«\s*([^»]{1,40}?)\s*»")),
    ("braces", re.compile(r"\{\{\s*([^}]{1,40}?)\s*\}\}")),
    ("bracket", re.compile(r"\[([A-Z][A-Za-z0-9 _/'-]{2,30})\]")),
)

_SLUG = re.compile(r"[^a-z0-9]+")

# A prefix inside a marker turns plain text into a richer slot. This is the
# whole authoring convention -- no Developer tab, no content controls, just
# typed into an ordinary document:
#   {{image: headshot}}                 a picture
#   {{check: agreed}}                   a tick box
#   {{choice: status | Draft, Final}}   pick one of a list
# and a dotted name, {{items.qty}}, is one column of a repeating row (see
# find_repeats). Anything with no prefix is text.
_IMAGE_MARKER = re.compile(r"^\s*(?:image|img)\s*:\s*(.+?)\s*$", re.I)
_CHECK_MARKER = re.compile(r"^\s*(?:check|checkbox|bool)\s*:\s*(.+?)\s*$", re.I)
_DATE_MARKER = re.compile(r"^\s*date\s*:\s*(.+?)\s*$", re.I)
_NUMBER_MARKER = re.compile(r"^\s*(?:number|num|amount)\s*:\s*(.+?)\s*$", re.I)
_CHOICE_MARKER = re.compile(
    r"^\s*(?:choice|select|pick)\s*:\s*([^|]+?)\s*(?:\|\s*(.*?))?\s*$", re.I)

# The typed prefixes that take a plain name (choice is handled apart, because
# its pipe carries options rather than a prompt).
_TYPED = ((IMAGE, _IMAGE_MARKER), (CHECKBOX, _CHECK_MARKER),
          (DATE, _DATE_MARKER), (NUMBER, _NUMBER_MARKER))


class Marker(NamedTuple):
    """What one marker means. `name` is cleaned of the `*` and `| ...` an author
    may add; `required` and `label` carry those back so the form can use them.
    For an image, `label` is the picture's alt text as well as its form label."""
    kind: str
    name: str
    choices: tuple[str, ...] = ()
    required: bool = False
    label: str = ""


def _split_prompt(body: str) -> tuple[str, str]:
    """A trailing `| words` is an author-written label/prompt for the field --
    and, for a picture, its alt text. Split it off the name."""
    if "|" in body:
        name, _, prompt = body.partition("|")
        return name.strip(), prompt.strip()
    return body.strip(), ""


def _required(name: str) -> tuple[str, bool]:
    """A trailing `*` on the name means the form must not leave it blank."""
    name = name.strip()
    if name.endswith("*"):
        return name[:-1].strip(), True
    return name, False


def marker_kind(inner: str) -> Marker:
    """Parse a marker's inner text into a `Marker`.

    The grammar, all optional except the name:
    ``[type:] name [*] [| label]`` -- a type prefix (image/check/date/number,
    or choice which instead lists options after the pipe), a trailing ``*`` for
    required, and a ``| label`` prompt. Shared by discovery and fill so the two
    never disagree on what a marker means.
    """
    hit = _CHOICE_MARKER.match(inner)
    if hit:
        options = tuple(o.strip() for o in (hit.group(2) or "").split(",")
                        if o.strip())
        name, required = _required(hit.group(1))
        return Marker(CHOICE, name, options, required, "")
    for kind, pattern in _TYPED:
        hit = pattern.match(inner)
        if hit:
            name, label = _split_prompt(hit.group(1))
            name, required = _required(name)
            return Marker(kind, name, (), required, label)
    name, label = _split_prompt(inner)
    name, required = _required(name)
    return Marker(TEXT, name, (), required, label)


def slug(text: str) -> str:
    stripped = _QUESTION_NUMBER.sub("", text.strip(), count=1)
    return _SLUG.sub("_", stripped.lower()).strip("_")[:48] or "field"


@dataclass
class FormField:
    kind: str = TEXT
    source: str = ""            # ffData | sdt | instruction | marker
    raw_name: str = ""          # what the document called it, if anything
    name: str = ""
    name_confidence: float = 0.0
    name_source: str = ""
    label: str = ""
    block_path: str = ""
    ordinal: int = 0            # which field of its kind within that block
    value: str = ""
    choices: tuple[str, ...] = ()
    required: bool = False
    needs_review: bool = False

    @property
    def filled(self) -> bool:
        return bool(self.value.strip()) and not _FIELD_FILLER.match(self.value)

    def describe(self) -> str:
        where = f" [{', '.join(self.choices)}]" if self.choices else ""
        return (f"{self.name} ({self.kind}){where} -- from {self.source}, "
                f"named by {self.name_source} ({self.name_confidence:.0%})")


@dataclass
class FormReport:
    fields: list[FormField] = field(default_factory=list)

    @property
    def unfilled(self) -> list[FormField]:
        return [f for f in self.fields if not f.filled and f.kind != CHECKBOX]

    def as_placeholders(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for item in self.fields:
            entry: dict = {"type": item.kind, "required": True,
                           "source": item.source}
            if item.choices:
                entry["choices"] = list(item.choices)
            if item.needs_review:
                entry["needs_review"] = True
            name, n = item.name, 1
            while name in out:
                n += 1
                name = f"{item.name}_{n}"
            out[name] = entry
        return out


# -- the label problem ----------------------------------------------------


class _Labels:
    """Where a reader would look for this field's name.

    On the first real form we tried -- a consular visa application -- the
    label was never in the obvious place. It was the line above inside the
    same cell, or the cell directly above in the same column, or, for a check
    box, the words printed after it in the same paragraph. So all three are
    tried, nearest first, and a field that finds none of them keeps its
    position for a name rather than borrowing a stranger's.
    """

    _CELL = re.compile(r"^(?P<table>.*tbl\[\d+\])/tr\[(?P<row>\d+)\]/tc\[(?P<col>\d+)\]")

    def __init__(self, blocks: Iterable[Block]):
        self.order: list[Block] = [b for b in blocks if b.is_paragraph]
        self._at: dict[str, int] = {b.path: i for i, b in enumerate(self.order)}
        self._cells: dict[tuple[str, int, int], list[Block]] = {}
        for block in self.order:
            if (key := self._cell_key(block.path)) is not None:
                self._cells.setdefault(key, []).append(block)

    @classmethod
    def _cell_key(cls, path: str):
        match = cls._CELL.match(path)
        if not match:
            return None
        return (match.group("table"), int(match.group("row")),
                int(match.group("col")))

    def for_block(self, block: Block) -> str:
        index = self._at.get(block.path)
        if index is None:
            return ""
        candidates: list[tuple[int, str]] = []
        key = self._cell_key(block.path)
        if key is not None:
            # Inside the cell first: "04 - Country of citizenship" sits above
            # its own field far more often than beside it.
            for other in self._cells.get(key, []):
                if other.path == block.path:
                    break
                if (text := self._usable(other)):
                    candidates.append((self._rank(text), text))
            candidates.extend(self._above(key))
        if not candidates:
            for other in reversed(self.order[:index]):
                if (text := self._usable(other)):
                    candidates.append((self._rank(text), text))
                    break
        if not candidates:
            return ""
        # A paragraph that looks like a label beats one that is merely
        # nearby. Without this the consular form named three fields after the
        # photo-size instructions printed in the cell beside them.
        best = max(rank for rank, _ in candidates)
        return next(text for rank, text in candidates if rank == best)

    def _above(self, key: tuple[str, int, int]) -> list[tuple[int, str]]:
        table, row, col = key
        for previous in range(row - 1, 0, -1):
            found = [
                (self._rank(text), text)
                for block in reversed(self._cells.get((table, previous, col), []))
                if (text := self._usable(block))
            ]
            if found:
                return found
        return []

    @staticmethod
    def _rank(text: str) -> int:
        """How much this reads like a label rather than like nearby prose."""
        if _QUESTION_NUMBER.match(text):
            return 2                      # "04 -  Country of citizenship"
        if text.rstrip().endswith((":", "\uff1a")):
            return 2                      # "Issuing country:"
        if len(text.split()) <= 6:
            return 1
        return 0

    @staticmethod
    def _usable(block: Block) -> str:
        if block.element.find(f".//{qn('w:ffData')}") is not None:
            return ""
        text = " ".join(block.text.split())
        if not text or _FIELD_FILLER.match(text) or len(text.split()) > 12:
            return ""
        return text


# -- the four declarations ------------------------------------------------


def _int(value: str | None) -> int:
    """A dropdown's selected index, tolerating a malformed non-numeric val."""
    try:
        return int(value) if value else 0
    except ValueError:
        return 0


def _ff_kind(data: etree._Element) -> tuple[str, tuple[str, ...], str]:
    if data.find(qn("w:checkBox")) is not None:
        checked = data.find(f"{qn('w:checkBox')}/{qn('w:checked')}")
        on = checked is not None and checked.get(qn("w:val")) not in ("0", "false")
        return CHECKBOX, (), ("checked" if on else "")
    ddlist = data.find(qn("w:ddList"))
    if ddlist is not None:
        entries = tuple(
            e.get(qn("w:val")) or ""
            for e in ddlist.findall(qn("w:listEntry"))
        )
        chosen = ddlist.find(qn("w:result"))
        index = _int(chosen.get(qn("w:val")) if chosen is not None else None)
        return CHOICE, entries, (entries[index] if index < len(entries) else "")
    default = data.find(f"{qn('w:textInput')}/{qn('w:default')}")
    return TEXT, (), (default.get(qn("w:val")) or "" if default is not None else "")


def _field_result(data: etree._Element) -> str:
    """What the field currently displays, which is not its default.

    `w:textInput/w:default` is what Word puts there when the form is reset.
    The result -- the runs between `separate` and `end` -- is what the person
    who filled the form actually typed, and it is the only one of the two
    that answers "is this field filled in?".
    """
    paragraph = data.getparent()
    while paragraph is not None and paragraph.tag != qn("w:p"):
        paragraph = paragraph.getparent()
    if paragraph is None:
        return ""
    runs = [r for r in paragraph.iter(qn("w:r"))]
    begin = data.getparent()
    while begin is not None and begin.tag != qn("w:r"):
        begin = begin.getparent()
    if begin is None or begin not in runs:
        return ""
    depth, collecting, out = 0, False, []
    for run in runs[runs.index(begin):]:
        for child in run:
            if child.tag == qn("w:fldChar"):
                kind = child.get(qn("w:fldCharType"))
                if kind == "begin":
                    depth += 1
                elif kind == "separate" and depth == 1:
                    collecting = True
                elif kind == "end":
                    depth -= 1
                    if depth == 0:
                        return " ".join("".join(out).split())
            elif child.tag == qn("w:t") and collecting and depth == 1:
                out.append(child.text or "")
    return " ".join("".join(out).split())


def _checkbox_labels(paragraph: etree._Element) -> list[tuple[str, str]]:
    """(text before, text after) for each check box in the paragraph.

    "[] male [] female" is two fields and two names, and taking the whole
    paragraph would call both of them "male female". Which side the label
    sits on is a house convention, not a rule -- Word's own Forms toolbar
    puts it after, and the consular form we tested against puts it before --
    so both are collected and the caller decides.
    """
    segments: list[str] = []
    boxes = 0
    pending: list[str] = []
    for node in paragraph.iter():
        tag = node.tag
        if not isinstance(tag, str):
            continue
        if tag == qn("w:ffData"):
            segments.append(" ".join("".join(pending).split()))
            pending = []
            boxes += 1
        elif tag == qn("w:t"):
            pending.append(node.text or "")
    segments.append(" ".join("".join(pending).split()))
    return [(segments[i], segments[i + 1]) for i in range(boxes)]


def _label_side(pairs: list[tuple[str, str]]) -> str:
    """Does this paragraph print its labels before its boxes, or after?

    The paragraph answers it itself. "[] male [] female" has nothing before
    the first box; "male [] female []" has nothing after the last. Guessing
    from Word's convention instead gets the consular form exactly wrong --
    every box takes the name of the option next to it rather than its own.
    """
    if not pairs:
        return "after"
    leading, trailing = pairs[0][0], pairs[-1][1]
    if not leading and trailing:
        return "after"
    if leading and not trailing:
        return "before"
    return "after"


def find_fields(pkg: OpcPackage) -> FormReport:
    """Every field this one document declares about itself."""
    report = FormReport()
    blocks = Walker(pkg).blocks()
    labels = _Labels(blocks)
    used: set[str] = set()

    for block in blocks:
        if not block.is_paragraph:
            continue
        _from_sdt(block, report)
        _from_ffdata(block, labels, report)
        _from_instructions(block, labels, report)
        _from_markers(block, report)
        _from_pictures(block, report)

    for item in report.fields:
        base = item.name or "field"
        name, n = base, 1
        while name in used:
            n += 1
            name = f"{base}_{n}"
        item.name = name
        used.add(name)
    return report


# -- repeating rows -------------------------------------------------------

# A dotted marker names a column of a repeating group: `{{items.qty}}` is the
# `qty` column of the `items` list. Kept deliberately plain -- a bare name on
# each side, no nesting -- so the whole rule is "same word before the dot means
# the same row, repeated once per record you supply".
_DOTTED = re.compile(r"^\s*([A-Za-z0-9_]+)\.([A-Za-z0-9_. -]+?)\s*$")


@dataclass
class RepeatColumn:
    name: str                       # the field after the dot, e.g. "qty"
    label: str                      # a human label, e.g. "Qty"
    kind: str = TEXT                # text | image | checkbox | choice
    choices: tuple[str, ...] = ()   # options, for a choice column


@dataclass
class RepeatGroup:
    name: str                                   # the collection, e.g. "items"
    columns: list[RepeatColumn] = field(default_factory=list)


def find_repeats(pkg: OpcPackage) -> list[RepeatGroup]:
    """The repeating groups a document declares, in first-seen order.

    One per collection named by a dotted marker; its columns are the fields
    after the dot. A column is whatever kind its marker is -- ``{{items.qty}}``
    is a text column, ``{{image: items.photo}}`` a picture column,
    ``{{check: items.done}}`` a tick box, ``{{choice: items.s | a, b}}`` a pick
    list -- so a repeating row can hold the same richness a single field can.
    The unit that repeats (a table row or a paragraph) is decided at fill time,
    so discovery here only has to know the shape of one record.
    """
    root = pkg.element(pkg.main_document)
    groups: dict[str, RepeatGroup] = {}
    seen_columns: dict[str, set] = {}
    for node in root.iter(qn("w:t")):
        text = node.text or ""
        if "." not in text:
            continue
        for _, pattern in _MARKERS:
            for match in pattern.finditer(text):
                marker = marker_kind(match.group(1).strip())
                dotted = _DOTTED.match(marker.name)
                if dotted is None:
                    continue
                collection = slug(dotted.group(1))
                column = dotted.group(2).strip()
                group = groups.setdefault(collection, RepeatGroup(collection))
                columns = seen_columns.setdefault(collection, set())
                if slug(column) not in columns:
                    columns.add(slug(column))
                    group.columns.append(RepeatColumn(
                        slug(column), marker.label or _prettify(column),
                        marker.kind, marker.choices))
    return list(groups.values())


def _prettify(text: str) -> str:
    return text.replace("_", " ").strip().title() or text


def _from_sdt(block: Block, report: FormReport) -> None:
    """Content controls, block-level and inline.

    Both shapes have to be handled and they are found in opposite
    directions. A block-level control *contains* the paragraph, so the
    walker's context carries its tag. An inline one sits *inside* the
    paragraph -- which is the shape `learn` itself writes, since a field is
    usually a few words in the middle of a line -- and is found by looking
    down rather than up.
    """
    context = block.context
    if context.in_sdt and context.sdt_tag:
        _add_control(block, context.sdt_tag, block.text, 0, report)
        return
    for ordinal, sdt in enumerate(block.element.iter(qn("w:sdt"))):
        props = sdt.find(qn("w:sdtPr"))
        if props is None:
            continue
        tag_el = props.find(qn("w:tag"))
        alias_el = props.find(qn("w:alias"))
        tag = (tag_el.get(qn("w:val")) if tag_el is not None else "") or \
              (alias_el.get(qn("w:val")) if alias_el is not None else "") or ""
        if not tag:
            continue
        content = sdt.find(qn("w:sdtContent"))
        showing = props.find(qn("w:showingPlcHdr")) is not None
        kind = _sdt_kind(props)
        # A picture control's "value" is an embedded image, not text; reading
        # its runs would give the placeholder graphic's alt text, not a value.
        value = "" if showing or kind == IMAGE else (
            "".join(content.itertext()) if content is not None else "")
        _add_control(block, tag, value, ordinal, report, kind=kind)


def _sdt_kind(props: etree._Element) -> str:
    """The field kind a content control's type element implies.

    Only the picture control is singled out -- it is the one that needs an
    image rather than text. Everything else stays text here; choices and
    check boxes are found more reliably from the legacy `w:ffData` path.
    """
    if props.find(qn("w:picture")) is not None:
        return IMAGE
    return TEXT


# -- pictures marked by their alt text ------------------------------------
#
# An author can turn a picture already in the document into an image field by
# writing a marker in its alt text (Word: right-click > Edit Alt Text). The
# picture stays -- its size, position, wrap and borders are the author's, and
# fill only swaps the image inside it. Bracket markers are excluded here: alt
# text is often prose, and `[a note]` should not silently become a field.
_PICTURE_MARKERS = tuple((name, pat) for name, pat in _MARKERS if name != "bracket")


def picture_marker(drawing: etree._Element) -> str | None:
    """The marker inner text written in a drawing's alt text, or None.

    Read from the drawing's `wp:docPr` (description and title) and the picture's
    own `pic:cNvPr` description -- wherever Word stores what a person types into
    the Alt Text box."""
    candidates: list[str] = []
    for doc_pr in drawing.iter(qn("wp:docPr")):
        candidates += [doc_pr.get("descr") or "", doc_pr.get("title") or ""]
    for cnv in drawing.iter(qn("pic:cNvPr")):
        candidates += [cnv.get("descr") or ""]
    for text in candidates:
        for _name, pattern in _PICTURE_MARKERS:
            hit = pattern.search(text)
            if hit is not None:
                return hit.group(1)
    return None


def in_content_control(element: etree._Element) -> bool:
    """Whether `element` sits inside a `w:sdt` -- a picture content control is
    handled by `_from_sdt`, so picture discovery skips its drawing."""
    node = element.getparent()
    while node is not None:
        if node.tag == qn("w:sdt"):
            return True
        node = node.getparent()
    return False


def _from_pictures(block: Block, report: FormReport) -> None:
    for drawing in block.element.iter(qn("w:drawing")):
        if in_content_control(drawing):
            continue
        inner = picture_marker(drawing)
        if inner is None:
            continue
        marker = marker_kind(inner.strip())
        # It is a picture, so it is an image field whatever the marker's prefix.
        report.fields.append(FormField(
            kind=IMAGE, source="picture", raw_name=inner[:60],
            name=slug(marker.name), name_confidence=0.9,
            name_source="a picture's alt text", label=marker.label,
            block_path=block.path, required=marker.required,
        ))


def _add_control(block: Block, tag: str, value: str, ordinal: int,
                 report: FormReport, kind: str = TEXT) -> None:
    name = tag[len("formgen."):] if tag.startswith("formgen.") else tag
    if any(f.source == "sdt" and f.raw_name == tag and
           f.block_path == block.path for f in report.fields):
        return
    report.fields.append(FormField(
        kind=kind, source="sdt", raw_name=tag, name=slug(name),
        name_confidence=1.0, name_source="the content control's own tag",
        block_path=block.path, ordinal=ordinal, value=value,
    ))


def _from_ffdata(block: Block, labels: _Labels, report: FormReport) -> None:
    found = list(block.element.iter(qn("w:ffData")))
    if not found:
        return
    # Not gated on there being several boxes: a list of options one to a
    # paragraph -- "[] no diploma", "[] bachelor's degree" -- is the most
    # common shape of all, and each of those paragraphs holds exactly one.
    per_box = _checkbox_labels(block.element)
    side = _label_side(per_box)
    nearby = labels.for_block(block)
    for index, data in enumerate(found):
        kind, choices, value = _ff_kind(data)
        if kind == TEXT:
            value = _field_result(data) or value
        name_el = data.find(qn("w:name"))
        raw = (name_el.get(qn("w:val")) or "") if name_el is not None else ""
        item = FormField(kind=kind, source="ffData", raw_name=raw,
                         choices=choices, block_path=block.path,
                         ordinal=index, value=value)
        # Only a check box takes its name from the words in its own
        # paragraph. For a text field that text is the field's *result* --
        # its value -- and naming a field after its contents works perfectly
        # on a blank form and renames every field the moment somebody fills
        # one in. We start from filled documents, so that is the normal case.
        if kind == CHECKBOX:
            before, after = per_box[index] if index < len(per_box) else ("", "")
            own = (after or before) if side == "after" else (before or after)
        else:
            own = ""
        if raw and not _AUTO_NAME.match(raw):
            item.name, item.name_confidence = slug(raw), 0.90
            item.name_source = "the field's own name"
        elif own:
            item.label = own
            item.name, item.name_confidence = slug(own), 0.70
            item.name_source = "the words printed beside it"
        elif nearby:
            item.label = nearby
            item.name, item.name_confidence = slug(nearby), 0.60
            item.name_source = f"the label {nearby!r}"
            item.needs_review = True
        else:
            item.name = slug(raw) if raw else f"field_{len(report.fields)}"
            item.name_confidence = 0.25
            item.name_source = "its position in the form"
            item.needs_review = True
        report.fields.append(item)


def _from_instructions(block: Block, labels: _Labels, report: FormReport) -> None:
    for instruction in field_instructions(block.element):
        match = _INSTRUCTION.match(instruction)
        if not match:
            continue
        kind, name = match.group(1).upper(), match.group(2).strip()
        report.fields.append(FormField(
            kind=TEXT, source="instruction", raw_name=instruction[:60],
            name=slug(name), name_confidence=0.95,
            name_source=f"a {kind} field", block_path=block.path,
            value=block.text,
        ))


def _from_markers(block: Block, report: FormReport) -> None:
    text = block.text
    if not text.strip():
        return
    ordinal = 0
    for style, pattern in _MARKERS:
        for match in pattern.finditer(text):
            marker = marker_kind(match.group(1).strip())
            if not marker.name or len(marker.name.split()) > 6:
                continue
            if _DOTTED.match(marker.name):
                continue  # a dotted name is a repeating-row column (find_repeats)
            ordinal += 1
            report.fields.append(FormField(
                kind=marker.kind, source="marker", raw_name=match.group(0),
                name=slug(marker.name), name_confidence=0.50,
                name_source=f"a {style} marker in the text",
                label=marker.label,  # author-written prompt / alt text, if any
                block_path=block.path, ordinal=ordinal - 1,
                choices=marker.choices, required=marker.required,
                needs_review=True,
            ))
