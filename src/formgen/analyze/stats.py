"""Consensus voting: how N documents agree, expressed as two numbers.

The single rule this module exists to enforce is that **"rare" and "contested"
are different**, and collapsing them into one confidence number is what makes
a learned format noisy enough that people stop trusting it. So every property
carries both:

    coverage  = documents that said anything at all / documents examined
    agreement = documents that said the modal thing / documents that said anything

A style appearing in 3 of 12 exemplars has coverage 0.25 and agreement 1.0:
that is a rare style used consistently, not a disagreement, and it should not
produce a lint rule that fires on nine documents.

Two further decisions are encoded here rather than left to callers:

**Voting is per document, not per observation.** A 300-paragraph report and a
5-paragraph memo get one vote each. Otherwise the longest exemplar decides the
house format on its own. Each document first votes internally (its own modal
value wins), and `internal_agreement` records how much it disagreed with
itself -- which is a direct measurement of accumulated direct formatting, and
therefore a donor-selection signal in its own right.

**Quantize before voting, report a raw value after.** 11.04pt and 11pt are the
same decision; bucketing merges them. But the value written into the donor is
the modal bucket's *median raw* observation, so the donor keeps a value some
author actually chose rather than a synthetic bucket centre.

Ties are broken deterministically (count, then the bucket's repr), because a
profile that changes when the exemplars are listed in a different order is not
an artifact anyone can diff.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Hashable, Iterable, Sequence

from ..oox.values import FontSize, Length, LineSpacing

# -- quantization ---------------------------------------------------------

# Bucket widths in each value's native unit, keyed by the final segment of a
# property pointer. The plan's numbers: font size 0.5pt, spacing 1pt, indents
# and page geometry 0.01in.
_TWIPS_PER_POINT = 20.0
_TWIPS_PER_HUNDREDTH_INCH = 14.4

GRANULARITY: dict[str, float] = {
    "space_before": _TWIPS_PER_POINT,
    "space_after": _TWIPS_PER_POINT,
    "char_spacing": _TWIPS_PER_POINT,
    "indent_left": _TWIPS_PER_HUNDREDTH_INCH,
    "indent_right": _TWIPS_PER_HUNDREDTH_INCH,
    "indent_first_line": _TWIPS_PER_HUNDREDTH_INCH,
    "indent_hanging": _TWIPS_PER_HUNDREDTH_INCH,
    "top": _TWIPS_PER_HUNDREDTH_INCH,
    "right": _TWIPS_PER_HUNDREDTH_INCH,
    "bottom": _TWIPS_PER_HUNDREDTH_INCH,
    "left": _TWIPS_PER_HUNDREDTH_INCH,
    "header": _TWIPS_PER_HUNDREDTH_INCH,
    "footer": _TWIPS_PER_HUNDREDTH_INCH,
    "gutter": _TWIPS_PER_HUNDREDTH_INCH,
    "width": _TWIPS_PER_HUNDREDTH_INCH,
    "height": _TWIPS_PER_HUNDREDTH_INCH,
}

# Default bucket for a Length nobody named: half a point. Tight enough that
# two visibly different values never merge.
DEFAULT_LENGTH_GRANULARITY = 10.0

# Line spacing in rule="auto" is 240ths of a line, so 12 twentieths is 0.05
# of a line -- finer than anyone sets deliberately, coarser than rounding noise.
LINE_GRANULARITY = 12.0


def bucket(value: Any, name: str = "") -> Hashable:
    """Quantize one observation into a comparable, hashable bucket.

    Returns a tagged tuple rather than a bare number so that a Length of 240
    can never collide with a FontSize of 240 in the same Counter.
    """
    if value is None:
        return None
    if isinstance(value, LineSpacing):
        # The rule is PART of the bucket. A line of 240 at rule="auto" is
        # single spacing; at rule="exact" it is 12pt. Letting those two share
        # a bucket is a 10x error dressed up as agreement.
        return ("line", value.rule, round(value.raw / LINE_GRANULARITY))
    if isinstance(value, FontSize):
        return ("sz", value.half_points)          # already 0.5pt granularity
    if isinstance(value, Length):
        gran = GRANULARITY.get(name, DEFAULT_LENGTH_GRANULARITY)
        return ("len", round(value.twips / gran))
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, (int, float)):
        return ("n", value)
    if isinstance(value, (tuple, list)):
        return ("t", tuple(bucket(v, name) for v in value))
    return ("r", repr(value))


def _representative(values: Sequence[Any]) -> Any:
    """The value to publish for a bucket: a median that was actually observed.

    median_low rather than median, so the donor never carries the average of
    two real values -- a number no author chose and no exemplar contains.
    """
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    try:
        return statistics.median_low(values)
    except TypeError:
        # Unorderable (mixed types in one bucket). Deterministic fallback.
        return sorted(values, key=repr)[len(values) // 2]


def _modal(counts: Counter) -> tuple[Hashable, int]:
    """Most common bucket; ties go to the smallest repr.

    Counter.most_common leaves ties in insertion order, which means the
    learned profile would change when the exemplars are listed in a different
    order. A profile nobody can diff is a profile nobody trusts.
    """
    if not counts:
        return None, 0
    return min(counts.items(), key=lambda kv: (-kv[1], repr(kv[0])))


# -- the vote -------------------------------------------------------------

STATUSES = (
    "normative",            # everyone has it, everyone agrees
    "optional_consistent",  # some have it, those that do agree
    "contested",            # everyone has it, a minority differs
    "variable",             # some have it and they differ
    "undecided",            # everyone has it and there is no majority
    "informational",        # too rare to say anything about
)

# What lint does with each status. "present" means the rule fires only when
# the property is set at all -- the right severity for something optional.
SEVERITY = {
    "normative": "error",
    "optional_consistent": "error-if-present",
    "contested": "warn",
    "variable": "info",
    "undecided": "off",
    "informational": "off",
}

# A status that is only a guess until someone confirms it.
NEEDS_REVIEW = frozenset({"undecided", "contested"})


@dataclass(frozen=True)
class DocumentVote:
    """One document's internal verdict on one property."""

    doc: str
    bucket: Hashable
    value: Any
    observations: int
    modal_observations: int

    @property
    def internal_agreement(self) -> float:
        """How consistent this document was with itself.

        Well below 1.0 means the author fought the styles with direct
        formatting, which disqualifies the document as a donor even when its
        modal value is the house one.
        """
        return self.modal_observations / self.observations if self.observations else 0.0


@dataclass(frozen=True)
class Vote:
    """The consensus on one property across a corpus."""

    key: str
    n_docs: int
    value: Any
    modal_bucket: Hashable
    observed_in: int
    modal_count: int
    alternatives: tuple[tuple[Hashable, int, Any], ...] = ()
    agreeing: tuple[str, ...] = ()
    dissenting: tuple[str, ...] = ()
    silent: tuple[str, ...] = ()
    per_doc: tuple[DocumentVote, ...] = field(default=(), repr=False)

    @property
    def coverage(self) -> float:
        return self.observed_in / self.n_docs if self.n_docs else 0.0

    @property
    def agreement(self) -> float:
        return self.modal_count / self.observed_in if self.observed_in else 0.0

    @property
    def status(self) -> str:
        coverage, agreement = self.coverage, self.agreement
        if coverage >= 0.75:
            if agreement >= 0.90:
                return "normative"
            return "contested" if agreement >= 0.50 else "undecided"
        if coverage >= 0.40:
            return "optional_consistent" if agreement >= 0.90 else "variable"
        return "informational"

    @property
    def severity(self) -> str:
        return SEVERITY[self.status]

    @property
    def in_donor(self) -> bool:
        """Should the donor carry this value?

        Everything except the merely informational: even an undecided
        property needs *a* value in a real .docx, and the mode is the least
        surprising one. What changes is whether lint enforces it.
        """
        return self.status != "informational"

    @property
    def needs_review(self) -> bool:
        return self.status in NEEDS_REVIEW

    @property
    def weak(self) -> bool:
        """True when too few documents spoke for the numbers to mean much.

        Two exemplars that agree give agreement 1.0, which reads as certainty
        it has not earned. Callers should surface this rather than hide it.
        """
        return self.observed_in < 3

    def explain(self) -> str:
        pct = f"{self.coverage:.0%} coverage, {self.agreement:.0%} agreement"
        detail = f"{self.modal_count}/{self.observed_in} of {self.n_docs} documents"
        note = " (weak: fewer than 3 observations)" if self.weak else ""
        return f"{self.key} = {self.value!s}  [{self.status}; {pct}; {detail}]{note}"


def vote_one(
    key: str,
    observations: Iterable[tuple[str, Any]],
    n_docs: int,
    docs: Sequence[str] = (),
) -> Vote:
    """Vote on one property.

    `observations` is (document id, raw value) pairs -- as many per document
    as the document contains. `n_docs` is the size of the whole corpus, which
    is NOT the same as the number of documents that said something; that
    difference is exactly what coverage measures.
    """
    name = key.rsplit("/", 1)[-1]
    by_doc: dict[str, list[Any]] = {}
    for doc, value in observations:
        if value is not None:
            by_doc.setdefault(doc, []).append(value)

    # Stage 1: each document votes internally, one vote each.
    per_doc: list[DocumentVote] = []
    for doc in sorted(by_doc):
        values = by_doc[doc]
        counts = Counter(bucket(v, name) for v in values)
        modal, count = _modal(counts)
        members = [v for v in values if bucket(v, name) == modal]
        per_doc.append(
            DocumentVote(doc, modal, _representative(members), len(values), count)
        )

    # Stage 2: one document, one vote.
    across = Counter(dv.bucket for dv in per_doc)
    modal, modal_count = _modal(across)
    winners = [dv for dv in per_doc if dv.bucket == modal]
    losers = [dv for dv in per_doc if dv.bucket != modal]

    alternatives = tuple(
        (b, c, _representative([dv.value for dv in per_doc if dv.bucket == b]))
        for b, c in sorted(
            across.items(), key=lambda kv: (-kv[1], repr(kv[0]))
        )
        if b != modal
    )
    spoke = {dv.doc for dv in per_doc}
    return Vote(
        key=key,
        n_docs=n_docs,
        value=_representative([dv.value for dv in winners]),
        modal_bucket=modal,
        observed_in=len(per_doc),
        modal_count=modal_count,
        alternatives=alternatives,
        agreeing=tuple(dv.doc for dv in winners),
        dissenting=tuple(dv.doc for dv in losers),
        silent=tuple(d for d in docs if d not in spoke),
        per_doc=tuple(per_doc),
    )


def vote_all(
    observations: dict[str, list[tuple[str, Any]]],
    docs: Sequence[str],
) -> dict[str, Vote]:
    """Vote on every property. Keys are JSON Pointers into the profile."""
    n = len(docs)
    return {key: vote_one(key, obs, n, docs) for key, obs in sorted(observations.items())}


def disagreement(a: dict[str, Vote], b: dict[str, Vote]) -> list[str]:
    """Keys where two vote sets reached different values -- for corpus drift."""
    return sorted(
        k for k in set(a) & set(b) if a[k].modal_bucket != b[k].modal_bucket
    )


def profile_distance(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Fraction of shared properties on which two documents differ.

    The metric behind outlier detection and the "your corpus is really two
    formats" report. Properties only one document has are excluded: a missing
    style is a coverage question, not a disagreement.
    """
    shared = set(a) & set(b)
    if not shared:
        return 1.0
    differing = sum(
        1 for k in shared if bucket(a[k], k.rsplit("/", 1)[-1])
        != bucket(b[k], k.rsplit("/", 1)[-1])
    )
    return differing / len(shared)
