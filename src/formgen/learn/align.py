"""Lining up N documents so their common skeleton falls out.

One giant Needleman-Wunsch over 400-block documents is both slow and produces
garbage columns, so the alignment is two-tier:

1. **Headings first.** They are short (10-40 per document) and high-signal, so
   aligning them is cheap and almost always right. A medoid document seeds the
   column set and the rest are aligned into it progressively.
2. **Then within each matched section**, the body blocks between two aligned
   headings -- a few dozen, not four hundred. A gap wider than
   `MAX_GAP` is declared free content rather than forced into an alignment
   that would be noise.

`difflib.SequenceMatcher` does the pairwise work. That is a deliberate choice
over a hand-rolled NW: it is in the standard library, it is deterministic, and
its "longest matching block first" strategy is patience-like -- it anchors on
what is unambiguous and only then looks at the gaps, which is exactly the
behaviour the two-tier design wants.

Determinism matters as much as quality here. A profile that comes out
different when the exemplars are listed in a different order is one nobody can
diff, so the medoid is chosen with an explicit tie-break and documents are
merged in sorted order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Hashable, Sequence

# Beyond this many unmatched blocks between two anchors, declare free content
# rather than forcing an alignment. The forced one would be noise, and noise
# in a skeleton becomes a lint rule that fires on everything.
MAX_GAP = 300

# A column present in this share of documents is part of the skeleton.
PRESENT = 0.75
OPTIONAL = 0.40


@dataclass(frozen=True, eq=False)
class Item:
    """One block, as the alignment sees it.

    Equality is `(kind, key)` and nothing else, so two documents' versions of
    the same boilerplate paragraph are the same token while the index and the
    raw text -- which every later stage needs and no alignment should ever
    compare -- ride along untouched.
    """

    kind: str
    key: str = ""
    index: int = -1
    text: str = ""
    sdt_tag: str = ""
    instruction: str = ""      # concatenated field instruction, if any

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Item):
            return NotImplemented
        return (self.kind, self.key) == (other.kind, other.key)

    def __hash__(self) -> int:
        return hash((self.kind, self.key))

    def __repr__(self) -> str:      # deterministic modal tie-break
        return f"Item({self.kind!r}, {self.key!r})"


def kind_of(token: Hashable) -> Hashable:
    if isinstance(token, Item):
        return token.kind
    return token[0] if isinstance(token, tuple) and token else token


def text_of(token: Hashable) -> str:
    if isinstance(token, Item):
        return token.text
    if isinstance(token, tuple) and len(token) > 1:
        return str(token[1])
    return ""


@dataclass
class Column:
    """One aligned position: which block each document put there, if any."""

    members: dict[str, int] = field(default_factory=dict)   # doc -> index
    tokens: dict[str, Hashable] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.members)

    def occupancy(self, total: int) -> float:
        return len(self.members) / total if total else 0.0

    def modal_token(self) -> Hashable:
        counts: dict[Hashable, int] = {}
        for token in self.tokens.values():
            counts[token] = counts.get(token, 0) + 1
        if not counts:
            return None
        return min(counts.items(), key=lambda kv: (-kv[1], repr(kv[0])))[0]

    def agreement(self) -> float:
        if not self.tokens:
            return 0.0
        modal = self.modal_token()
        same = sum(1 for token in self.tokens.values() if token == modal)
        return same / len(self.tokens)


def similarity(a: Sequence[Hashable], b: Sequence[Hashable]) -> float:
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, list(a), list(b), autojunk=False).ratio()


def medoid(sequences: dict[str, Sequence[Hashable]]) -> str:
    """The document most like all the others -- the best seed for the columns.

    Ties break on the document id so the same corpus always produces the same
    skeleton whatever order it was listed in.
    """
    names = sorted(sequences)
    if len(names) == 1:
        return names[0]
    scores = {
        name: sum(similarity(sequences[name], sequences[other])
                  for other in names if other != name)
        for name in names
    }
    return max(names, key=lambda name: (scores[name], -names.index(name)))


def align(sequences: dict[str, Sequence[Hashable]]) -> list[Column]:
    """Progressive multiple alignment of N token sequences."""
    names = sorted(sequences)
    if not names:
        return []
    seed = medoid(sequences)
    columns = [
        Column(members={seed: index}, tokens={seed: token})
        for index, token in enumerate(sequences[seed])
    ]
    for name in names:
        if name == seed:
            continue
        columns = _merge(columns, name, sequences[name])
    return columns


def _merge(columns: list[Column], name: str,
           sequence: Sequence[Hashable]) -> list[Column]:
    consensus = [column.modal_token() for column in columns]
    matcher = SequenceMatcher(None, consensus, list(sequence), autojunk=False)
    merged: list[Column] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                column = columns[i1 + offset]
                column.members[name] = j1 + offset
                column.tokens[name] = sequence[j1 + offset]
                merged.append(column)
        elif tag == "delete":
            merged.extend(columns[i1:i2])
        elif tag == "insert":
            merged.extend(_new_columns(name, sequence, j1, j2))
        else:
            merged.extend(_replace(columns[i1:i2], name, sequence, j1, j2))
    return merged


# Alignment on full tokens finds boilerplate, because boilerplate is the only
# thing textually identical across documents. It cannot find a *placeholder*,
# whose whole nature is that every document says something different there --
# and a placeholder that lands in a one-member column is one the profile never
# learns. So a mismatch is retried on kinds alone.
coarse = kind_of


def _replace(columns: list[Column], name: str, sequence: Sequence[Hashable],
             j1: int, j2: int) -> list[Column]:
    """Both sides have blocks here and they differ.

    Re-run the match on kinds alone: a heading opposite a heading, or a
    table cell opposite the same table cell, is the same slot holding
    different text -- exactly the shape of a placeholder. Anything still
    unmatched at that resolution really is two different blocks, and both
    are kept.
    """
    left = [coarse(column.modal_token()) for column in columns]
    right = [coarse(token) for token in sequence[j1:j2]]
    merged: list[Column] = []
    for tag, a1, a2, b1, b2 in SequenceMatcher(
            None, left, right, autojunk=False).get_opcodes():
        if tag == "equal":
            for offset in range(a2 - a1):
                column = columns[a1 + offset]
                index = j1 + b1 + offset
                column.members[name] = index
                column.tokens[name] = sequence[index]
                merged.append(column)
        elif tag == "delete":
            merged.extend(columns[a1:a2])
        elif tag == "insert":
            merged.extend(_new_columns(name, sequence, j1 + b1, j1 + b2))
        else:
            merged.extend(columns[a1:a2])
            merged.extend(_new_columns(name, sequence, j1 + b1, j1 + b2))
    return merged


def _new_columns(name: str, sequence: Sequence[Hashable],
                 start: int, stop: int) -> list[Column]:
    return [
        Column(members={name: index}, tokens={name: sequence[index]})
        for index in range(start, stop)
    ]


# -- the two tiers --------------------------------------------------------


@dataclass
class Section:
    """A run of body blocks between two aligned headings."""

    heading: Column | None
    columns: list[Column] = field(default_factory=list)
    free: bool = False          # the gap was too wide to align meaningfully

    @property
    def title(self) -> str:
        return text_of(self.heading.modal_token()) if self.heading else ""


@dataclass
class Skeleton:
    sections: list[Section] = field(default_factory=list)
    documents: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def columns(self) -> list[Column]:
        out: list[Column] = []
        for section in self.sections:
            if section.heading is not None:
                out.append(section.heading)
            out.extend(section.columns)
        return out

    def required_headings(self) -> list[Column]:
        total = len(self.documents)
        return [
            section.heading for section in self.sections
            if section.heading is not None
            and section.heading.occupancy(total) >= PRESENT
        ]


def build_skeleton(
    headings: dict[str, Sequence[Hashable]],
    bodies: dict[str, dict[int, Sequence[Hashable]]],
) -> Skeleton:
    """Align headings, then align the body of each matched section.

    `bodies[doc][i]` is the block sequence that follows the document's i-th
    heading. Splitting the problem this way is what keeps the second tier's
    inputs small enough for an alignment to mean something.
    """
    names = sorted(headings)
    skeleton = Skeleton(documents=tuple(names))
    heading_columns = align(headings)

    # Everything before the first heading is a section of its own: on most
    # documents that is the cover page, which is where the placeholders are.
    preamble = {
        name: bodies.get(name, {}).get(-1, []) for name in names
    }
    skeleton.sections.append(_section(None, preamble, skeleton))

    for column in heading_columns:
        section_bodies = {
            name: bodies.get(name, {}).get(index, [])
            for name, index in column.members.items()
        }
        skeleton.sections.append(_section(column, section_bodies, skeleton))
    return skeleton


def _section(heading: Column | None, bodies: dict[str, Sequence[Hashable]],
             skeleton: Skeleton) -> Section:
    widest = max((len(sequence) for sequence in bodies.values()), default=0)
    if widest > MAX_GAP:
        skeleton.warnings.append(
            f"a section runs to {widest} blocks in at least one document; "
            "it is treated as free content rather than aligned, because an "
            "alignment that wide produces noise rather than structure."
        )
        return Section(heading=heading, columns=[], free=True)
    return Section(heading=heading, columns=align(bodies) if bodies else [])


# -- intra-paragraph splitting -------------------------------------------


@dataclass
class Split:
    """One column's text broken into the literal parts and the varying part."""

    prefix: str = ""
    suffix: str = ""
    values: dict[str, str] = field(default_factory=dict)

    @property
    def is_field(self) -> bool:
        return bool(self.values) and len({*self.values.values()}) > 1

    def template(self, name: str = "value") -> str:
        return f"{self.prefix}{{{name}}}{self.suffix}"


def split_variable_part(texts: dict[str, str]) -> Split | None:
    """Find the literal frame around a varying value.

    "Report No. LR-2024-0041" and "Report No. LR-2025-0118" share the prefix
    "Report No. " and differ after it; that prefix is boilerplate and what
    follows is the field. This is where most real placeholders live, and it
    is why a whole-paragraph comparison finds nothing useful on a cover page.
    """
    values = {k: v for k, v in texts.items() if v.strip()}
    if len(values) < 2:
        return None
    names = sorted(values)
    words = {name: values[name].split() for name in names}

    prefix = _common_prefix([words[name] for name in names])
    suffix = _common_suffix([words[name][len(prefix):] for name in names])
    if not prefix and not suffix:
        return None
    middles = {
        name: " ".join(words[name][len(prefix):len(words[name]) - len(suffix)])
        for name in names
    }
    if all(not middle for middle in middles.values()):
        return None
    return Split(
        prefix=(" ".join(prefix) + " ") if prefix else "",
        suffix=(" " + " ".join(suffix)) if suffix else "",
        values=middles,
    )


def _common_prefix(sequences: list[list[str]]) -> list[str]:
    if not sequences:
        return []
    out: list[str] = []
    for index in range(min(len(s) for s in sequences)):
        token = sequences[0][index]
        if all(s[index] == token for s in sequences):
            out.append(token)
        else:
            break
    return out


def _common_suffix(sequences: list[list[str]]) -> list[str]:
    reversed_common = _common_prefix([list(reversed(s)) for s in sequences])
    return list(reversed(reversed_common))
