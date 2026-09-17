"""Learn a template from filled examples, so nobody has to type markers.

Show formgen two or three *filled* copies of the same form and it finds the
fields by noticing what differs: the parts that change from copy to copy are the
answers, the parts that stay are the form. It takes the first copy as the base
-- keeping its exact formatting, styles and layout -- and replaces each varying
span with a ``{{marker}}``, named from the static label that precedes it. The
result is a real template you can fill or hand to the fill form.

This is the authoring shortcut for a normal person: instead of learning a marker
syntax and editing a document by hand, they point at examples they already have.

Scope: it compares the main document body paragraph by paragraph, so the samples
must share a layout (they are copies of one blank). Repeating tables whose row
counts differ are flagged rather than guessed at -- a v1 boundary, not a value.
"""

from __future__ import annotations

import difflib
import re

from ..opc.ns import qn
from ..opc.package import OpcPackage
from .formfields import CHECKBOX, DATE, NUMBER, TEXT, slug

_DATEISH = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}/\d{1,2}/\d{2,4}")
_NUMBERISH = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")
_BALLOT = {"☒", "☐"}   # ☒ ☐
# A real field boundary carries a word of label between the two values; a bare
# space, dash or shared digit does not -- that is one value that happens to
# share characters across the samples (a date's dashes, a name's spaces).
_LABELISH = re.compile(r"[A-Za-z]{2,}")
# Characters that belong to a value rather than a label (digits and the
# punctuation of dates and numbers). A varying span grows over these where they
# are shared, so "2026-01-01"/"2025-02-02" (sharing "20") is captured whole
# instead of leaving "20" frozen into the form -- but never over a colon or
# space, which separate a label from its value.
_VALUEISH = set("0123456789/-.,")


def learn_template(docs: list[bytes]) -> tuple[bytes, dict]:
    """Derive a marked-up template from filled copies of one form.

    Returns the template ``.docx`` bytes and an info dict: ``fields`` (name and
    kind, in document order) and ``warnings`` (anything the caller should know,
    e.g. mismatched layouts).
    """
    if len(docs) < 2:
        raise ValueError("learning a template needs at least two filled copies")

    packages = [_open(d) for d in docs]
    base = packages[0]
    base_root = base.element(base.main_document)
    base_paras = _paragraphs(base_root)
    other_paras = [_paragraphs(p.element(p.main_document)) for p in packages[1:]]

    warnings: list[str] = []
    if any(len(op) != len(base_paras) for op in other_paras):
        warnings.append("the samples don't line up paragraph for paragraph; "
                        "comparing as far as they do. Use copies of one blank "
                        "form for the cleanest result.")

    fields: list[dict] = []
    used: set[str] = set()
    for index, paragraph in enumerate(base_paras):
        others = [op[index] for op in other_paras if index < len(op)]
        if not others:
            continue
        base_text = _para_text(paragraph)
        texts = [_para_text(o) for o in others]
        if all(text == base_text for text in texts):
            continue
        stable, ranges = _varying(base_text, texts)
        # Right to left, so a replacement never shifts a not-yet-done span;
        # collect this paragraph's fields and restore reading order after.
        here: list[dict] = []
        for start, end in reversed(ranges):
            label = _stable_prefix(base_text, stable, start)
            name = _unique(slug(_label_words(label)) or "field", used)
            kind = _infer_kind(base_text[start:end])
            _replace_range(paragraph, start, end, _marker(kind, name))
            here.append({"name": name, "kind": kind})
        fields.extend(reversed(here))

    base.touch(base.main_document)
    _strip_session(base)
    return _save(base), {"fields": fields, "warnings": warnings,
                         "samples": len(docs)}


# -- paragraph text -------------------------------------------------------


def _paragraphs(root):
    return [p for p in root.iter(qn("w:p"))]


def _segments(paragraph):
    segments, pos = [], 0
    for node in paragraph.iter(qn("w:t")):
        parent = node.getparent()
        if parent is None or parent.tag != qn("w:r"):
            continue
        text = node.text or ""
        segments.append((node, pos, pos + len(text)))
        pos += len(text)
    return segments


def _para_text(paragraph) -> str:
    return "".join(text for _, text in (
        (node, node.text or "") for node in paragraph.iter(qn("w:t"))
        if node.getparent() is not None
        and node.getparent().tag == qn("w:r")))


def _replace_range(paragraph, start: int, end: int, replacement: str) -> None:
    """Replace the characters [start, end) of a paragraph's run text with
    `replacement`, put into the first run involved (the rest cleared) -- the
    inverse of filling a marker."""
    involved = [s for s in _segments(paragraph) if s[1] < end and s[2] > start]
    if not involved:
        return
    first_node, first_start, _ = involved[0]
    last_node, last_start, _ = involved[-1]
    head = (first_node.text or "")[: start - first_start]
    tail = (last_node.text or "")[end - last_start:]
    if first_node is last_node:            # the whole span is one run
        first_node.text = head + replacement + tail
        first_node.set(qn("xml:space"), "preserve")
        return
    first_node.text = head + replacement
    first_node.set(qn("xml:space"), "preserve")
    for middle_node, _, _ in involved[1:-1]:
        middle_node.text = ""
    last_node.text = tail


# -- the diff -------------------------------------------------------------


def _varying(base_text: str, texts: list[str]):
    """Which positions of `base_text` are stable across every other text, and
    the maximal ranges that are not. A position is stable only if it lands in an
    'equal' block against *all* the others."""
    stable = set(range(len(base_text)))
    for other in texts:
        equal: set[int] = set()
        matcher = difflib.SequenceMatcher(None, base_text, other, autojunk=False)
        for a, _b, size in matcher.get_matching_blocks():
            equal.update(range(a, a + size))
        stable &= equal

    ranges, i, length = [], 0, len(base_text)
    while i < length:
        if i in stable:
            i += 1
            continue
        j = i
        while j < length and j not in stable:
            j += 1
        ranges.append((i, j))
        i = j
    return stable, _expand(base_text, stable, _merge(base_text, ranges))


def _expand(base_text: str, stable: set[int], ranges: list) -> list:
    """Grow each span over adjacent shared value-characters, then re-merge any
    that now touch -- so a date or number's shared digits join the field."""
    grown = []
    for start, end in ranges:
        while start > 0 and (start - 1) in stable and base_text[start - 1] in _VALUEISH:
            start -= 1
        while end < len(base_text) and end in stable and base_text[end] in _VALUEISH:
            end += 1
        grown.append([start, end])
    grown.sort()
    out = [grown[0]] if grown else []
    for start, end in grown[1:]:
        if start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(s, e) for s, e in out]


def _merge(base_text: str, ranges: list) -> list:
    """Join varying spans that are one value split by incidental shared
    characters -- merge across a between-gap that holds no word of label."""
    if not ranges:
        return ranges
    merged = [list(ranges[0])]
    for start, end in ranges[1:]:
        between = base_text[merged[-1][1]:start]
        if _LABELISH.search(between):
            merged.append([start, end])
        else:
            merged[-1][1] = end
    return [(s, e) for s, e in merged]


def _stable_prefix(base_text: str, stable: set[int], start: int) -> str:
    """The run of stable (form) text immediately before `start` -- the label
    the field sits next to, with any earlier field value excluded."""
    k = start
    while k > 0 and (k - 1) in stable:
        k -= 1
    return base_text[k:start]


def _label_words(label: str) -> str:
    # A label is real words next to the field ("Date of birth:"). Take the
    # trailing run of words of two letters or more, which skips a stray letter
    # or digit that a coincidental character overlap left in the stable text.
    trimmed = label.strip().rstrip(":-–—").strip()
    words = re.findall(r"[A-Za-z]{2,}", trimmed)
    return "_".join(words[-4:])


def _unique(name: str, used: set[str]) -> str:
    candidate, n = name, 1
    while candidate in used:
        n += 1
        candidate = f"{name}_{n}"
    used.add(candidate)
    return candidate


def _infer_kind(value: str) -> str:
    text = value.strip()
    if text in _BALLOT:
        return CHECKBOX
    if _DATEISH.fullmatch(text):
        return DATE
    if _NUMBERISH.fullmatch(text):
        return NUMBER
    return TEXT


def _marker(kind: str, name: str) -> str:
    if kind == CHECKBOX:
        return f"{{{{check: {name}}}}}"
    if kind == DATE:
        return f"{{{{date: {name}}}}}"
    if kind == NUMBER:
        return f"{{{{number: {name}}}}}"
    return f"{{{{{name}}}}}"


# -- package helpers ------------------------------------------------------


def _open(data: bytes) -> OpcPackage:
    import tempfile
    from pathlib import Path
    # A directory, not NamedTemporaryFile -- Windows forbids reopening the temp
    # file while its own handle is open. `open` reads the bytes into memory.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "in.docx"
        path.write_bytes(data)
        return OpcPackage.open(path)


def _save(pkg: OpcPackage) -> bytes:
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as out:
        target = Path(out) / "template.docx"
        pkg.save(target, deterministic=True)
        return target.read_bytes()


def _strip_session(pkg: OpcPackage) -> None:
    from ..content.session import SESSION_PART
    if SESSION_PART in pkg:
        pkg.drop_part(SESSION_PART)
