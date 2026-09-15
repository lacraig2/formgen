"""Turn N ballots into one profile, and say honestly how sure we are.

Everything here is downstream of `analyze.stats`, which owns the arithmetic.
What this module adds is the corpus-level judgement:

* **which documents are outliers** -- a document that agrees with less than
  60% of the consensus is probably not the same format, and averaging it in
  quietly degrades every rule;
* **whether the corpus is really two formats** -- the most likely real-world
  surprise, because "our house format" usually means "the 2019 template and
  the 2023 one". Saying so is far more useful than emitting a profile that
  splits the difference and matches neither;
* **what a human must confirm** before the profile can be trusted.

The clustering is a two-medoid search rather than k-means, done exhaustively:
corpora are tens of documents, not thousands, and an exhaustive search is
deterministic where k-means with a random seed is not. A profile that changes
between runs on the same inputs is not an artifact anyone can diff, and that
property is worth more here than asymptotic elegance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Sequence

from ..analyze.stats import Vote, profile_distance, vote_all
from .observe import DocObservations

# A document agreeing with less than this share of the consensus is reported
# as a possible outlier rather than silently averaged in.
OUTLIER_AGREEMENT = 0.60

# How much tighter the two clusters must be than the corpus as a whole before
# we claim the corpus is really two formats. Below this it is just spread.
SPLIT_STRENGTH = 1.5

# Below this many exemplars, every number in the profile is a guess with a
# percentage sign on it.
MIN_CORPUS = 3


@dataclass
class Cluster:
    medoid: str
    members: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.members)


@dataclass
class Split:
    """A corpus that looks like two formats rather than one."""

    clusters: tuple[Cluster, Cluster]
    strength: float

    def describe(self) -> str:
        a, b = self.clusters
        return (
            f"the corpus splits into {len(a)} + {len(b)} documents "
            f"(separation {self.strength:.1f}x). "
            f"Group A: {', '.join(a.members)}. Group B: {', '.join(b.members)}."
        )


@dataclass
class Consensus:
    """The learned profile, plus everything a human needs to judge it."""

    votes: dict[str, Vote]
    docs: tuple[str, ...]
    agreement_by_doc: dict[str, float] = field(default_factory=dict)
    outliers: tuple[str, ...] = ()
    split: Split | None = None
    warnings: tuple[str, ...] = ()

    def __len__(self) -> int:
        return len(self.votes)

    def value(self, pointer: str, default: Any = None) -> Any:
        vote = self.votes.get(pointer)
        return vote.value if vote is not None else default

    def by_status(self, *statuses: str) -> dict[str, Vote]:
        return {k: v for k, v in self.votes.items() if v.status in statuses}

    def donor_values(self) -> dict[str, Any]:
        """What the donor should carry: everything but the merely rare."""
        return {k: v.value for k, v in self.votes.items() if v.in_donor}

    def needs_review(self) -> dict[str, Vote]:
        return {k: v for k, v in self.votes.items() if v.needs_review}

    def enforceable(self) -> dict[str, Vote]:
        return {k: v for k, v in self.votes.items() if v.severity != "off"}


def build(observations: Sequence[DocObservations]) -> Consensus:
    """Vote, then judge the vote."""
    docs = tuple(o.doc for o in observations)
    ballots: dict[str, list[tuple[str, Any]]] = {}
    for obs in observations:
        for pointer, doc, value in obs.ballots():
            ballots.setdefault(pointer, []).append((doc, value))

    votes = vote_all(ballots, docs)
    agreement = _agreement_by_doc(votes, docs)
    outliers = tuple(
        d for d in docs if agreement.get(d, 1.0) < OUTLIER_AGREEMENT
    )
    split = _find_split({o.doc: o.single() for o in observations})

    warnings: list[str] = []
    if len(docs) < MIN_CORPUS:
        warnings.append(
            f"only {len(docs)} exemplar(s): every agreement figure below is "
            f"{100 / max(len(docs), 1):.0f}% granular, and a unanimous vote "
            "means very little. Add exemplars before trusting the profile."
        )
    for doc in outliers:
        warnings.append(
            f"{doc} agrees with only {agreement[doc]:.0%} of the consensus; "
            "it may not be the same format. Check it, or drop it and re-learn."
        )
    if split is not None:
        warnings.append(split.describe())
    undecided = [k for k, v in votes.items() if v.status == "undecided"]
    if undecided:
        warnings.append(
            f"{len(undecided)} propert(y/ies) have no majority at all and are "
            "linted off until confirmed; see the needs-review list."
        )
    return Consensus(
        votes=votes,
        docs=docs,
        agreement_by_doc=agreement,
        outliers=outliers,
        split=split,
        warnings=tuple(warnings),
    )


def _agreement_by_doc(votes: dict[str, Vote], docs: Sequence[str]) -> dict[str, float]:
    """Share of the votes a document took part in where it backed the winner.

    Votes the document was silent on are excluded: not having a style is not
    disagreeing about it, and counting silence as dissent would make every
    short exemplar look like an outlier.
    """
    spoke: dict[str, int] = {d: 0 for d in docs}
    agreed: dict[str, int] = {d: 0 for d in docs}
    for vote in votes.values():
        for doc in vote.agreeing:
            spoke[doc] = spoke.get(doc, 0) + 1
            agreed[doc] = agreed.get(doc, 0) + 1
        for doc in vote.dissenting:
            spoke[doc] = spoke.get(doc, 0) + 1
    return {
        d: (agreed[d] / spoke[d] if spoke[d] else 1.0) for d in docs
    }


def distance_matrix(profiles: dict[str, dict[str, Any]]) -> dict[tuple[str, str], float]:
    names = sorted(profiles)
    out: dict[tuple[str, str], float] = {}
    for a, b in combinations(names, 2):
        d = profile_distance(profiles[a], profiles[b])
        out[(a, b)] = out[(b, a)] = d
    for name in names:
        out[(name, name)] = 0.0
    return out


def _find_split(profiles: dict[str, dict[str, Any]]) -> Split | None:
    """Exhaustive two-medoid search. Deterministic by construction."""
    names = sorted(profiles)
    if len(names) < 4:
        # Two clusters of at least two documents each is the smallest claim
        # worth making; below that a "split" is just two documents.
        return None
    dist = distance_matrix(profiles)

    best: tuple[float, str, str] | None = None
    for a, b in combinations(names, 2):
        cost = sum(min(dist[(n, a)], dist[(n, b)]) for n in names)
        if best is None or cost < best[0]:
            best = (cost, a, b)
    assert best is not None
    _, medoid_a, medoid_b = best

    group_a = tuple(n for n in names if dist[(n, medoid_a)] <= dist[(n, medoid_b)])
    group_b = tuple(n for n in names if n not in group_a)
    if len(group_a) < 2 or len(group_b) < 2:
        return None

    within = [dist[(x, y)] for g in (group_a, group_b) for x, y in combinations(g, 2)]
    between = [dist[(x, y)] for x in group_a for y in group_b]
    mean_within = sum(within) / len(within) if within else 0.0
    mean_between = sum(between) / len(between) if between else 0.0
    if mean_within == 0.0:
        # Two internally identical groups that differ at all is a clean split.
        strength = float("inf") if mean_between > 0 else 0.0
    else:
        strength = mean_between / mean_within
    if strength < SPLIT_STRENGTH:
        return None
    return Split(
        clusters=(Cluster(medoid_a, group_a), Cluster(medoid_b, group_b)),
        strength=strength,
    )
