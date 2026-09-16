"""Deciding what each aligned column *is*, and what to call it.

Three kinds of column come out of the alignment:

* **BOILERPLATE** -- every document says the same thing. The text is pinned
  and lint enforces it verbatim, which is what a distribution statement or a
  classification marking needs.
* **PLACEHOLDER** -- every document has something there and they differ. This
  is a field, and it becomes a content control in the donor.
* **FREE_CONTENT** -- every document has something there, they differ, and
  what they have is prose. The style is pinned; the text is the author's.

**The honest part is the uncertainty.** Optional-versus-missing is undecidable
from a corpus: a column present in 8 of 12 documents is either an optional
section or a mandatory one four authors forgot, and nothing in the data
distinguishes them. Everything in that band is marked `needs_review` rather
than guessed at, and a profile whose required slots are not confidently named
fails the build -- non-zero exit, artifact still written -- so a human is
forced through the review exactly once rather than never.

Naming runs in priority order, and the confidence attached to each source is
what decides whether a human has to look:

    w:sdt tag  >  DOCPROPERTY field  >  a document property with the same
    value  >  the label next to it  >  a positional slug
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .align import (OPTIONAL, PRESENT, Column, Item, Skeleton, Split,
                    kind_of, split_variable_part, text_of)

BOILERPLATE = "boilerplate"
PLACEHOLDER = "placeholder"
FREE_CONTENT = "free_content"

# Roles whose text belongs to the document, never to the format. Three
# exemplars will happily agree on a title, and enforcing that would tell
# every author to rename their report.
NOT_BOILERPLATE_ROLES = {"title", "caption", "toc"}

# Text agreement at or above this means "they all say the same thing".
VERBATIM = 0.95
# A field's value is short. Longer than this and it is prose, not a field.
FIELD_WORDS = 12
# With no literal wording around it, "short" has to mean much shorter. A
# framed value is a field because the frame says so -- "Report No. " and then
# whatever follows. An unframed column is only a field if the values
# themselves look like values, and an eleven-word sentence does not.
BARE_FIELD_WORDS = 6
# Below this, a name is a guess and a human must confirm it.
NAME_CONFIDENCE = 0.70

_SLUG = re.compile(r"[^a-z0-9]+")
_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z0-9 /&'.-]{1,40}?)\s*[:–-]\s*$")
_DOC_PROPERTY = re.compile(r'DOCPROPERTY\s+"?([A-Za-z0-9_ .-]+)"?', re.I)


def slug(text: str) -> str:
    return _SLUG.sub("_", text.strip().lower()).strip("_") or "field"


@dataclass
class Slot:
    """One column of the skeleton, classified and named."""

    index: int
    kind: str
    occupancy: float
    agreement: float
    role: str = "body"
    section: str = ""                   # the heading this slot lives under
    is_heading: bool = False
    text: str = ""                      # the pinned text, for boilerplate
    name: str = ""
    name_confidence: float = 0.0
    name_source: str = ""
    value_type: str = "text"
    pattern: str = ""
    examples: tuple[str, ...] = ()
    split: Split | None = None
    # Where this slot lives in the donor, so it can be made into a real
    # content control there. The donor is the one document whose blocks we
    # are allowed to edit.
    donor_index: int | None = None
    donor_value: str = ""
    optional: bool = False
    # Whether lint holds a document to this slot's exact wording. Decided
    # here, once, so that what `learn` prints and what `lint` enforces cannot
    # drift apart.
    enforced: bool = False
    needs_review: bool = False
    notes: tuple[str, ...] = ()

    @property
    def is_placeholder(self) -> bool:
        return self.kind == PLACEHOLDER

    def describe(self) -> str:
        where = "optional " if self.optional else ""
        if self.kind == PLACEHOLDER:
            return (
                f"{where}placeholder {self.name!r} ({self.value_type}) "
                f"-- {self.occupancy:.0%} coverage, named from "
                f"{self.name_source} ({self.name_confidence:.0%})"
            )
        if self.kind == BOILERPLATE:
            return f"{where}boilerplate: {self.text[:60]!r}"
        return f"{where}free content ({self.role})"


@dataclass
class SkeletonProfile:
    slots: list[Slot] = field(default_factory=list)
    documents: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def placeholders(self) -> list[Slot]:
        return [s for s in self.slots if s.is_placeholder]

    @property
    def required_sections(self) -> list[Slot]:
        """Headings the corpus agrees every document has, in order."""
        return [s for s in self.slots if s.is_heading and not s.optional]

    @property
    def boilerplate(self) -> list[Slot]:
        """Fixed wording lint will hold a document to, word for word.

        Enforced where fixed wording actually lives: on the cover and in the
        front matter, before the first heading. A passage every exemplar
        shares *there* is a distribution statement, a classification marking
        or a standard disclaimer. Under a heading it is prose several
        exemplars happened to share, which a small corpus produces
        constantly, and enforcing that would tell authors their Introduction
        is wrong for not matching last quarter's.
        """
        return [s for s in self.slots if s.kind == BOILERPLATE and s.enforced]

    def as_json(self) -> dict:
        return {
            "documents": list(self.documents),
            "slots": [_slot_json(s) for s in self.slots],
            "warnings": list(self.warnings),
        }

    @property
    def unconfident(self) -> list[Slot]:
        return [
            s for s in self.placeholders
            if not s.optional and s.name_confidence < NAME_CONFIDENCE
        ]

    def as_overrides(self) -> dict[str, dict]:
        """The placeholders section of overrides.yaml."""
        out: dict[str, dict] = {}
        for slot in self.placeholders:
            entry: dict[str, Any] = {
                "type": slot.value_type,
                "required": not slot.optional,
            }
            if slot.pattern:
                entry["pattern"] = slot.pattern
            if slot.examples:
                entry["examples"] = list(slot.examples[:3])
            if slot.needs_review:
                entry["needs_review"] = True
            if slot.split:
                # The frame is how a re-learn finds this field again.
                entry["template"] = slot.split.template(slot.name)
            out[slot.name] = entry
        return out


def _slot_json(slot: Slot) -> dict:
    row: dict[str, Any] = {
        "index": slot.index,
        "kind": slot.kind,
        "role": slot.role,
        "section": slot.section,
        "heading": slot.is_heading,
        "coverage": round(slot.occupancy, 4),
        "agreement": round(slot.agreement, 4),
        "optional": slot.optional,
    }
    if slot.needs_review:
        row["needs_review"] = True
    if slot.notes:
        row["notes"] = list(slot.notes)
    if slot.kind == BOILERPLATE:
        row["text"] = slot.text
        row["enforced"] = slot.enforced
    if slot.kind == PLACEHOLDER:
        row.update({
            "name": slot.name,
            "type": slot.value_type,
            "name_confidence": round(slot.name_confidence, 3),
            "name_source": slot.name_source,
        })
        if slot.pattern:
            row["pattern"] = slot.pattern
        if slot.examples:
            row["examples"] = list(slot.examples[:3])
        if slot.split:
            row["template"] = slot.split.template(slot.name)
    return row


def classify(
    skeleton: Skeleton,
    properties: dict[str, dict[str, str]] | None = None,
    donor: str | None = None,
    known: dict[str, dict] | None = None,
) -> SkeletonProfile:
    """Turn aligned columns into named, typed slots.

    `properties` is each document's core and custom document properties. A
    placeholder whose value equals one of them across the corpus *is* that
    property -- the strongest naming signal short of the document saying so
    itself with a content control.

    `known` is `overrides.yaml`'s placeholder section: names a human has
    already looked at and kept. Re-learning with a bigger corpus must not
    quietly rename their field back to whatever we guessed the first time,
    so those names win outright.
    """
    profile = SkeletonProfile(documents=skeleton.documents)
    total = len(skeleton.documents)
    previous_text = ""
    index = 0

    for section in skeleton.sections:
        title = section.title
        pairs = ([(section.heading, True)] if section.heading is not None else [])
        pairs += [(column, False) for column in section.columns]
        for column, is_heading in pairs:
            column_texts = {doc: text_of(token)
                            for doc, token in column.tokens.items()}
            slot = _classify_column(index, column, column_texts, total)
            slot.section = title
            slot.is_heading = is_heading
            slot.enforced = (
                slot.kind == BOILERPLATE and not is_heading
                and not slot.optional
                and slot.role not in NOT_BOILERPLATE_ROLES
                and not title.strip()
            )
            if section.free and not is_heading:
                slot.kind = FREE_CONTENT
                slot.notes += ("the section was too long to align",)
            if slot.kind == PLACEHOLDER:
                _name(slot, column_texts, previous_text, properties or {},
                      column, known or {})
                _type(slot, column_texts)
            # Every slot, not only the placeholders: knowing which of the
            # donor's own blocks a column came from is what lets the donor be
            # stripped back to the format. A slot with no donor index is one
            # the donor did not contribute to, and redaction reads the absence
            # as "the corpus never vouched for this block", so populating it
            # only for placeholders would blank the boilerplate.
            _locate_in_donor(slot, column, donor)
            profile.slots.append(slot)
            previous_text = _modal(column_texts)
            index += 1

    _dedupe_names(profile)
    profile.warnings.extend(skeleton.warnings)
    for slot in profile.unconfident:
        profile.warnings.append(
            f"placeholder {slot.name!r} was named from {slot.name_source} "
            f"({slot.name_confidence:.0%} confidence) -- confirm it in "
            "template.docx before relying on it."
        )
    return profile


def _classify_column(index: int, column: Column, texts: dict[str, str],
                     total: int) -> Slot:
    occupancy = column.occupancy(total)
    present = [t for t in texts.values() if t.strip()]
    agreement = _agreement(present)
    optional = occupancy < PRESENT
    role = _modal_role(column)

    slot = Slot(index=index, kind=FREE_CONTENT, occupancy=occupancy,
                agreement=agreement, role=role, optional=optional)
    if occupancy < OPTIONAL:
        slot.kind = FREE_CONTENT
        slot.notes += ("too rare to be part of the skeleton",)
        return slot

    if agreement >= VERBATIM and present:
        slot.kind = BOILERPLATE
        slot.text = _modal(texts)
        return slot

    split = split_variable_part(texts)
    if split is not None and split.is_field and _framed_values_look_like_values(split):
        slot.kind = PLACEHOLDER
        slot.split = split
        slot.text = split.prefix + split.suffix
        return slot

    if present and all(_value_shaped(t) for t in present):
        slot.kind = PLACEHOLDER
        return slot

    slot.kind = FREE_CONTENT
    if optional:
        # Undecidable from the corpus: an optional section, or a mandatory one
        # that four authors forgot. Saying which would be inventing evidence.
        slot.needs_review = True
        slot.notes += (
            f"present in {occupancy:.0%} of documents -- optional, or "
            "mandatory and often omitted? The corpus cannot say.",
        )
    return slot


_SENTENCE_END = (".", "?", "!", ":", ";")


def _value_shaped(text: str) -> bool:
    """Does this look like something somebody filled in, or something they wrote?

    Two tests, and the second does most of the work: a value does not end in
    a full stop. "L. Craig" and "Draft" are fields; "The panel was soaked at
    340 K." is a sentence, and typing a sentence into a content control is
    not what anyone means by a placeholder.
    """
    stripped = text.strip()
    if len(stripped.split()) > BARE_FIELD_WORDS:
        return False
    return not stripped.endswith(_SENTENCE_END)


def _framed_values_look_like_values(split: Split) -> bool:
    """A shared prefix is evidence of a label, but it can be a coincidence.

    Six findings that happen to open with "The" share a prefix, and trusting
    the frame alone turns an author's paragraph into a field called `the`.
    The frame buys a longer allowance -- a label really is evidence -- but
    not the sentence test: a value does not end in a full stop, and prose
    does.
    """
    values = [v.strip() for v in split.values.values() if v.strip()]
    if not values:
        return False
    return all(len(v.split()) <= FIELD_WORDS and not v.endswith(_SENTENCE_END)
               for v in values)


def _agreement(texts: list[str]) -> float:
    if not texts:
        return 0.0
    counts = Counter(texts)
    return counts.most_common(1)[0][1] / len(texts)


def _modal(texts: dict[str, str]) -> str:
    present = [t for t in texts.values() if t.strip()]
    if not present:
        return ""
    counts = Counter(present)
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def _modal_role(column: Column) -> str:
    found = [str(kind_of(token)) for token in column.tokens.values()]
    if not found:
        return "body"
    counts = Counter(found)
    return min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]


# -- naming ---------------------------------------------------------------


def _name(slot: Slot, texts: dict[str, str], previous_text: str,
          properties: dict[str, dict[str, str]], column: Column,
          known: dict[str, dict]) -> None:
    values = slot.split.values if slot.split else {
        doc: text.strip() for doc, text in texts.items() if text.strip()
    }
    slot.examples = tuple(sorted({v for v in values.values() if v}))[:5]

    confirmed = _already_named(slot, known)
    if confirmed:
        slot.name, slot.name_source, slot.name_confidence = (
            confirmed, "a name you already confirmed", 1.0
        )
        return
    tag = _sdt_tag(column)
    if tag:
        slot.name, slot.name_source, slot.name_confidence = (
            tag, "a content control in the document", 1.0
        )
        return
    field_name = _doc_property_field(column)
    if field_name:
        slot.name, slot.name_source, slot.name_confidence = (
            slug(field_name), "a DOCPROPERTY field", 0.95
        )
        return
    matched = _matching_property(values, properties)
    if matched:
        slot.name, slot.name_source, slot.name_confidence = (
            slug(matched), f"the document property {matched!r}", 0.85
        )
        return
    label = _label_before(slot, previous_text)
    if label:
        slot.name, slot.name_source, slot.name_confidence = (
            slug(label), f"the label {label!r} beside it", 0.60
        )
        slot.needs_review = True
        return
    slot.name = f"field_{slot.index}"
    slot.name_source = "its position in the document"
    slot.name_confidence = 0.30
    slot.needs_review = True


def _already_named(slot: Slot, known: dict[str, dict]) -> str:
    """A field the user has already named, recognised by its literal frame.

    The exemplars carry no content controls, so a re-learn cannot join on the
    tag; it joins on the wording around the value, which is the one thing
    that is the same in the corpus and in the template. Without this, a user
    who renames a field in Word loses the rename the next time the format is
    learned -- the exact failure that stops people trusting a tool.
    """
    if not slot.split:
        return ""
    for name, entry in sorted(known.items()):
        template = entry.get("template")
        if not template:
            continue
        marker = "{" + name + "}"
        if marker not in template:
            continue
        head, _, tail = template.partition(marker)
        if head.strip() == slot.split.prefix.strip() and \
                tail.strip() == slot.split.suffix.strip():
            return name
    return ""


def _sdt_tag(column: Column) -> str:
    """The document already said what this is. Nothing outranks that."""
    tags = sorted(
        token.sdt_tag for token in column.tokens.values()
        if isinstance(token, Item) and token.sdt_tag
    )
    for tag in tags:
        if tag.startswith("formgen."):
            return tag[len("formgen."):]
    return tags[0] if tags else ""


def _doc_property_field(column: Column) -> str:
    """`DOCPROPERTY "ProjectNumber"` names itself.

    The instruction arrives already concatenated across its runs -- Word
    splits them at arbitrary points, so `DOCPROP` + `ERTY "Proj` +
    `ectNumber"` is normal and matching run by run finds nothing.
    """
    for token in sorted(column.tokens.values(), key=repr):
        instruction = token.instruction if isinstance(token, Item) else ""
        match = _DOC_PROPERTY.search(instruction)
        if match:
            return match.group(1).strip()
    return ""


def _matching_property(values: dict[str, str],
                       properties: dict[str, dict[str, str]]) -> str:
    """A field whose value IS a document property is that property.

    Stronger than any text heuristic, because the author filled both in and
    Word kept them in step.
    """
    if not properties or not values:
        return ""
    candidates: Counter = Counter()
    for doc, value in values.items():
        for name, property_value in (properties.get(doc) or {}).items():
            if value and property_value and value.strip() == property_value.strip():
                candidates[name] += 1
    if not candidates:
        return ""
    name, hits = min(candidates.items(), key=lambda kv: (-kv[1], kv[0]))
    return name if hits >= max(2, len(values) // 2) else ""


def _label_before(slot: Slot, previous_text: str) -> str:
    """The label in the cell or paragraph before -- "Report Number:".

    Weak on purpose: it is right often enough to be worth offering and wrong
    often enough that the result is marked for review.
    """
    if slot.split and slot.split.prefix.strip():
        candidate = slot.split.prefix.strip().rstrip(":–-").strip()
        if candidate:
            return candidate
    match = _LABEL.match(previous_text or "")
    return match.group(1) if match else ""


def _dedupe_names(profile: SkeletonProfile) -> None:
    seen: dict[str, int] = {}
    for slot in profile.placeholders:
        base = slot.name
        if base not in seen:
            seen[base] = 1
            continue
        seen[base] += 1
        slot.name = f"{base}_{seen[base]}"
        slot.needs_review = True
        slot.notes += (f"two columns both wanted the name {base!r}",)


def _locate_in_donor(slot: Slot, column: Column, donor: str | None) -> None:
    """Where in the donor this slot's block actually is.

    `Column.members` holds the position within the sequence that was
    aligned -- one section's body, say -- not the position in the document.
    The two coincide only when that sequence starts at the first block, which
    is why this read right for a corpus whose cover page was plain paragraphs
    and silently addressed the wrong block the moment there was a title above
    a table. The Item carries the document index; use it.
    """
    if donor is None or donor not in column.tokens:
        return
    token = column.tokens[donor]
    slot.donor_index = getattr(token, "index", None)
    if slot.donor_index is not None and slot.donor_index < 0:
        slot.donor_index = None
    if slot.split and donor in slot.split.values:
        slot.donor_value = slot.split.values[donor]
    else:
        slot.donor_value = text_of(token).strip()


# -- type inference -------------------------------------------------------

_SHAPE = re.compile(r"[A-Z]+|[a-z]+|[0-9]+|[^A-Za-z0-9]+")
_PERSON = re.compile(r"^[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){1,3}$")


def _type(slot: Slot, texts: dict[str, str]) -> None:
    values = [v for v in (slot.split.values if slot.split else {
        d: t.strip() for d, t in texts.items()
    }).values() if v]
    if not values:
        return
    if _all_dates(values):
        slot.value_type = "date"
        return
    if all(_is_number(v) for v in values):
        slot.value_type = "number"
        return

    distinct = sorted(set(values))
    if len(distinct) <= 5 and len(values) >= 2 * len(distinct) and len(values) >= 4:
        slot.value_type = "enum"
        slot.examples = tuple(distinct)
        return
    if all(_PERSON.match(v) for v in values):
        slot.value_type = "person"
        return
    pattern = _shared_pattern(values)
    if pattern:
        slot.value_type = "identifier"
        slot.pattern = pattern
        return
    slot.value_type = "text"


_MONTH = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", re.I)
_SEPARATED = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")


def _date_shaped(value: str) -> bool:
    """A gate in front of dateutil, which is far too willing.

    ``dateutil.parse("12.4")`` succeeds -- it reads it as the 4th of December
    -- so a column of measurements would be typed as dates and linted against
    a date format for the rest of the profile's life. Require the value to
    actually look like a date before asking.
    """
    if _MONTH.search(value):
        return True
    return any(_SEPARATED.match(part) for part in value.split())


def _all_dates(values: list[str]) -> bool:
    try:
        from dateutil import parser as date_parser
    except ImportError:      # pragma: no cover - dateutil ships with Anaconda
        return False
    if any(len(v.split()) > 5 for v in values):
        return False
    for value in values:
        if not _date_shaped(value):
            return False
        try:
            date_parser.parse(value, fuzzy=False)
        except (ValueError, OverflowError, TypeError):
            return False
    return True


def _is_number(value: str) -> bool:
    try:
        float(value.replace(",", ""))
    except ValueError:
        return False
    return True


def _shared_pattern(values: list[str]) -> str:
    """A regex derived from the shape every example shares.

    LR-2024-0041 and LR-2026-0142 give ``[A-Z]{2}-\\d{4}-\\d{4}``. It is
    offered as a lint rule and written into overrides.yaml where a human can
    loosen it, because a pattern inferred from four examples is a hypothesis.
    """
    shapes = [_SHAPE.findall(value) for value in values]
    if len({len(shape) for shape in shapes}) != 1:
        return ""
    parts: list[str] = []
    for position in range(len(shapes[0])):
        pieces = [shape[position] for shape in shapes]
        kinds = {_kind(piece) for piece in pieces}
        if len(kinds) != 1:
            return ""
        kind = kinds.pop()
        lengths = {len(piece) for piece in pieces}
        if kind == "literal":
            if len(set(pieces)) != 1:
                return ""
            parts.append(re.escape(pieces[0]))
            continue
        quantifier = (f"{{{lengths.pop()}}}" if len(lengths) == 1
                      else f"{{{min(len(p) for p in pieces)},"
                           f"{max(len(p) for p in pieces)}}}")
        parts.append({"upper": "[A-Z]", "lower": "[a-z]", "digit": r"\d"}[kind]
                     + quantifier)
    pattern = "".join(parts)
    return f"^{pattern}$" if any(c in pattern for c in "[\\") else ""


def _kind(piece: str) -> str:
    if piece.isdigit():
        return "digit"
    if piece.isupper() and piece.isalpha():
        return "upper"
    if piece.islower() and piece.isalpha():
        return "lower"
    return "literal"
