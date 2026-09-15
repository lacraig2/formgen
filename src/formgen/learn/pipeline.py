"""`formgen learn` end to end: exemplars in, a correctable profile out.

The order here is load-bearing. Consensus is computed from *all* exemplars
before a donor is chosen, because donor scoring asks "how well does this
document agree with the others?" -- a question that cannot be answered until
the others have voted. And the donor is re-opened from disk rather than reused
from the observation pass, so that the package we clone is the untouched
original: observation walks the tree, and a donor is a byte-faithful copy or
it is not a donor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from ..errors import InputError, RefusalError, UsageError
from ..opc.errors import PackageError
from ..opc.package import OpcPackage
from ..profile import io as pio
from ..profile.sync import placeholders_in
from .consensus import Consensus, build
from .donor import DonorScore, ScrubReport, rank, scrub
from .materialize import MaterializeReport, materialize
from .observe import DocObservations, observe
from .placeholders import SkeletonProfile, classify as classify_slots
from .skeleton import properties_for, skeleton_for


@dataclass
class LearnResult:
    directory: Path
    consensus: Consensus
    observations: list[DocObservations]
    ranking: list[DonorScore]
    donor: DonorScore | None = None
    scrub_report: ScrubReport | None = None
    template_sha: str | None = None
    skeleton: SkeletonProfile | None = None
    materialized: MaterializeReport | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> int:
        return len(self.consensus.needs_review())

    @property
    def unconfident(self) -> list:
        """Required placeholders whose name is a guess.

        A non-empty list fails the build. The artifacts are still written --
        refusing to write them would leave nothing to correct -- but the exit
        code forces a human through the review exactly once, rather than
        never.
        """
        return self.skeleton.unconfident if self.skeleton else []


def _doc_ids(paths: Sequence[Path]) -> list[str]:
    """Stable, unique, human-meaningful ids. Two a.docx in different folders
    must not collapse into one ballot."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for path in paths:
        stem = path.stem
        seen[stem] = seen.get(stem, 0) + 1
        out.append(stem if seen[stem] == 1 else f"{stem}#{seen[stem]}")
    return out


def load_corpus(paths: Sequence[Path]) -> list[DocObservations]:
    if not paths:
        raise UsageError("no exemplars given", "point learn at some .docx files")
    observations: list[DocObservations] = []
    for path, doc in zip(paths, _doc_ids(paths)):
        try:
            pkg = OpcPackage.open(path)
        except PackageError as exc:
            raise InputError(f"{path.name}: {exc}") from exc
        obs = observe(pkg, doc)
        obs.meta.sha256 = pio.sha256_of(path)
        observations.append(obs)
    return observations


def learn(
    paths: Sequence[Path],
    directory: Path,
    name: str | None = None,
    generated: str | None = None,
) -> LearnResult:
    paths = [Path(p) for p in paths]
    directory = Path(directory)
    observations = load_corpus(paths)
    consensus = build(observations)
    ranking = rank(observations, consensus)
    result = LearnResult(
        directory=directory,
        consensus=consensus,
        observations=observations,
        ranking=ranking,
    )

    best = ranking[0] if ranking else None
    if best is None or best.disqualified:
        reasons = "; ".join(best.disqualified) if best else "no candidates"
        raise RefusalError(
            f"no exemplar can serve as the donor ({reasons})",
            "every candidate is disqualified. Resolve the reason above in at "
            "least one document -- usually accepting tracked changes or "
            "removing protection -- and re-run.",
        )
    result.donor = best

    source = paths[[o.doc for o in observations].index(best.doc)]
    donor_pkg = OpcPackage.open(source)
    result.scrub_report = scrub(donor_pkg)

    # The skeleton pass runs on the *scrubbed* donor rather than the original,
    # so that the block indices the alignment records still address the same
    # paragraphs when the placeholders are written back into it.
    # Load the corrections before inferring, not after: a field the user has
    # already named must keep that name, or re-learning with a bigger corpus
    # silently undoes their work.
    overrides = pio.Overrides.load(directory / pio.OVERRIDES)
    result.skeleton, result.materialized = _infer_skeleton(
        paths, observations, best.doc, donor_pkg, overrides.placeholders)

    directory.mkdir(parents=True, exist_ok=True)
    template = directory / pio.TEMPLATE
    donor_pkg.save(template, deterministic=True)
    result.template_sha = pio.sha256_of(template)

    # Content controls already in the donor ARE placeholders; record them now
    # so the very first sync has something to diff against rather than
    # reporting every one of them as newly added.
    # Inference goes in first: it is the only source that knows a field's
    # type, its pattern and whether it is required. The donor scan then fills
    # in what it alone knows -- the control's tag and Word-facing label --
    # and adds any control a user inserted by hand that inference missed.
    if result.skeleton is not None:
        for pname, entry in result.skeleton.as_overrides().items():
            overrides.placeholders.setdefault(pname, {}).update(entry)
    for pname, entry in placeholders_in(donor_pkg).items():
        known = overrides.placeholders.setdefault(pname, {})
        for key, value in entry.items():
            known.setdefault(key, value)

    result.notes = pio.write_profile(
        directory=directory,
        name=name or directory.name,
        consensus=consensus,
        overrides=overrides,
        metas=[o.meta for o in observations],
        donor=best,
        template_sha=result.template_sha,
        generated=generated,
        skeleton=result.skeleton,
    )
    result.notes.extend(_donor_notes(donor_pkg, consensus))
    if result.skeleton is not None:
        result.notes.extend(result.skeleton.warnings)
        note = result.materialized.note() if result.materialized else None
        if note:
            result.notes.append(note)
    return result


MIN_CORPUS_FOR_SKELETON = 3


def _infer_skeleton(paths, observations, donor_doc, donor_pkg, known=None):
    """Align the corpus and classify the columns.

    Below three exemplars there is nothing to align: two documents agree on
    everything they share, so every column comes out either boilerplate or a
    field, and both answers are noise. Saying "not enough documents" is the
    honest result.
    """
    docs = [o.doc for o in observations]
    if len(docs) < MIN_CORPUS_FOR_SKELETON:
        return None, None

    corpus: dict[str, OpcPackage] = {}
    properties: dict[str, dict[str, str]] = {}
    for path, doc in zip(paths, docs):
        pkg = donor_pkg if doc == donor_doc else OpcPackage.open(path)
        corpus[doc] = pkg
        properties[doc] = properties_for(pkg)

    contexts: dict = {}
    aligned = skeleton_for(corpus, contexts)
    profile = classify_slots(aligned, properties=properties, donor=donor_doc,
                             known=known)
    report = materialize(contexts[donor_doc], profile, donor_doc)
    return profile, report


def _donor_notes(pkg: OpcPackage, consensus: Consensus) -> list[str]:
    """Honest reporting of what the donor does not carry.

    A referenced-but-undefined style falls back to Word's own built-in
    definition, which varies by Word version and locale -- so a role the
    consensus names but the donor does not define is a real gap, not a
    cosmetic one.
    """
    from ..oox.styles import StyleGraph, normalize_style_name
    from ..opc.ns import RT

    styles_part = pkg.related(RT["styles"])
    if not styles_part or styles_part not in pkg:
        return ["the donor has no styles part; the profile cannot be applied."]
    graph = StyleGraph.parse(pkg.element(styles_part))
    defined = {normalize_style_name(s.name) for s in graph.styles.values()}
    wanted = {
        p.split("/")[3]
        for p, v in consensus.votes.items()
        if p.startswith("/styles/") and v.in_donor and len(p.split("/")) > 3
    }
    missing = sorted(wanted - defined)
    notes: list[str] = []
    if missing:
        notes.append(
            f"the donor does not define {len(missing)} style(s) the corpus "
            f"uses ({', '.join(missing[:4])}"
            + (", ..." if len(missing) > 4 else "")
            + "). Word would fall back to its own built-in definitions, which "
            "vary by version and locale -- define them in template.docx."
        )
    return notes
