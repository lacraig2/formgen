"""Choosing the exemplar that becomes template.docx, and cleaning it up.

The donor is the format. Everything Word understands and JSON does not --
latent styles, conditional table formatting, linked character styles, compat
settings, embedded fonts -- rides along in it byte-for-byte, which is why the
donor is cloned and pruned rather than synthesised.

**The counter-intuitive scoring rule is the direct-formatting penalty.** The
exemplar that looks most correct is often the one whose authors got it to look
correct by selecting text and pressing buttons. Clone that and you inherit a
styles.xml nobody was using: every document built from it starts by fighting
the same battle. A slightly plainer document whose look comes from its styles
is a far better template, so direct-formatting density is weighted heavily and
negatively.

Scrubbing then removes what is personal, stale, or actively hostile to the
recipient: editing-session ids, spell-check caches, author names, comments,
protection, and an attached template pointing at a UNC path -- inert for us,
but it hangs the next person's Word on open while it looks for a share they
cannot reach.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from lxml import etree

from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from .consensus import Consensus
from .observe import DocObservations

# Weights. They sum to 1.0 before the penalty, which is subtracted, so a
# document made entirely of direct formatting cannot win on coverage alone.
W_AGREEMENT = 0.45
W_ROLE_COVERAGE = 0.30
W_COMPLETENESS = 0.25
W_DIRECT_PENALTY = 0.60


@dataclass
class DonorScore:
    doc: str
    agreement: float = 0.0
    role_coverage: float = 0.0
    completeness: float = 0.0
    direct_density: float = 0.0
    disqualified: tuple[str, ...] = ()

    @property
    def total(self) -> float:
        if self.disqualified:
            return float("-inf")
        return (
            W_AGREEMENT * self.agreement
            + W_ROLE_COVERAGE * self.role_coverage
            + W_COMPLETENESS * self.completeness
            - W_DIRECT_PENALTY * self.direct_density
        )

    def explain(self) -> str:
        if self.disqualified:
            return f"{self.doc}: disqualified -- {self.disqualified[0]}"
        return (
            f"{self.doc}: {self.total:+.3f}  "
            f"(agreement {self.agreement:.0%}, styles {self.role_coverage:.0%}, "
            f"completeness {self.completeness:.0%}, "
            f"direct formatting {self.direct_density:.0%})"
        )


def score(obs: DocObservations, consensus: Consensus) -> DonorScore:
    meta = obs.meta
    wanted = _consensus_styles(consensus)
    have = set(meta.styles_used)
    role_coverage = len(wanted & have) / len(wanted) if wanted else 1.0

    # Completeness: does this document exercise the parts of a format that
    # cannot be recovered from a document which lacks them? A donor with no
    # header part gives us nothing to graft.
    signals = (
        meta.header_parts > 0,
        meta.footer_parts > 0,
        meta.list_count > 0,
        meta.section_count >= 1,
        meta.paragraphs >= 10,
    )
    return DonorScore(
        doc=meta.doc,
        agreement=consensus.agreement_by_doc.get(meta.doc, 0.0),
        role_coverage=role_coverage,
        completeness=sum(signals) / len(signals),
        direct_density=meta.direct_density,
        disqualified=meta.disqualified,
    )


def _consensus_styles(consensus: Consensus) -> set[str]:
    """Style names the consensus considers part of the format."""
    out: set[str] = set()
    for pointer, vote in consensus.votes.items():
        if not pointer.startswith("/styles/") or not vote.in_donor:
            continue
        parts = pointer.split("/")
        if len(parts) > 3:
            out.add(parts[3])
    return out


def rank(
    observations: Sequence[DocObservations], consensus: Consensus
) -> list[DonorScore]:
    """All candidates, best first. Ties break on document id for determinism."""
    scores = [score(o, consensus) for o in observations]
    return sorted(scores, key=lambda s: (-s.total, s.doc))


def select(
    observations: Sequence[DocObservations], consensus: Consensus
) -> DonorScore | None:
    ranked = rank(observations, consensus)
    return ranked[0] if ranked and ranked[0].disqualified == () else None


# -- the scrub ------------------------------------------------------------


@dataclass
class ScrubReport:
    removed: dict[str, int] = field(default_factory=dict)
    dropped_parts: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    # The names and company this document was labelled with. Kept -- not to
    # put back, but so redaction can look for them in the text it decided to
    # KEEP. A corpus written by one person has that person's name in every
    # footer, so the corpus agrees on it and consensus calls it house
    # boilerplate. Only the scrubbed identity can tell us otherwise.
    identities: tuple[str, ...] = ()

    def count(self, what: str, n: int = 1) -> None:
        if n:
            self.removed[what] = self.removed.get(what, 0) + n

    def summary(self) -> str:
        if not self.removed and not self.dropped_parts:
            return "nothing to scrub"
        bits = [f"{n} {what}" for what, n in sorted(self.removed.items())]
        if self.dropped_parts:
            bits.append(f"{len(self.dropped_parts)} parts")
        return "removed " + ", ".join(bits)


_RSID_ATTRS = ("rsid", "rsidR", "rsidRPr", "rsidDel", "rsidP", "rsidRDefault",
               "rsidSect", "rsidTr")
_COMMENT_RELTYPES = ("comments", "commentsExtended", "commentsIds", "people")


def scrub(pkg: OpcPackage) -> ScrubReport:
    """Strip identity, session state and hostile settings from a donor clone."""
    report = ScrubReport()
    main = pkg.main_document

    parts = [main]
    parts += [p for t in ("header", "footer") for p in dict.fromkeys(pkg.related_all(RT[t]))]
    for reltype in ("styles", "numbering", "footnotes", "endnotes"):
        if (p := pkg.related(RT[reltype])) and p in pkg:
            parts.append(p)

    for part in parts:
        root = pkg.edit(part)
        report.count("editing-session ids", _strip_rsids(root))
        report.count("spell-check markers", _strip_tags(root, "w:proofErr"))
        report.count("content-control locks", _strip_tags(root, "w:lock"))
        # A bound control silently reverts anything written into it unless the
        # custom XML is updated too. Dropping the binding makes the control
        # editable and makes `new` write one place instead of two.
        report.count("data bindings", _strip_tags(root, "w:dataBinding"))

    report.count("comment anchors", _strip_comments(pkg, main))
    _scrub_settings(pkg, report)
    _scrub_core_properties(pkg, report)

    dropped = []
    for reltype in _COMMENT_RELTYPES:
        for part in dict.fromkeys(pkg.related_all(RT[reltype])):
            pkg.drop_part(part)
            dropped.append(part)
    report.dropped_parts = tuple(dropped)
    return report


def _strip_rsids(root: etree._Element) -> int:
    """Revision-save ids: a per-editing-session fingerprint on every element.

    They are also the largest single source of noise in a docx diff, so
    removing them is what makes a donor reviewable at all.
    """
    n = 0
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for name in _RSID_ATTRS:
            key = qn(f"w:{name}")
            if key in el.attrib:
                del el.attrib[key]
                n += 1
    return n


def _strip_tags(root: etree._Element, *tags: str) -> int:
    n = 0
    for tag in tags:
        for el in list(root.iter(qn(tag))):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)
                n += 1
    return n


def _strip_comments(pkg: OpcPackage, main: str) -> int:
    """Remove comment anchors from the body.

    The runs carrying w:commentReference go too: a run whose only child is the
    reference renders as nothing, but it still counts as a run everywhere we
    measure runs, and it keeps a dangling id alive after the parts are gone.
    """
    root = pkg.edit(main)
    n = _strip_tags(root, "w:commentRangeStart", "w:commentRangeEnd")
    for ref in list(root.iter(qn("w:commentReference"))):
        run = ref.getparent()
        if run is not None and run.tag == qn("w:r"):
            target, parent = run, run.getparent()
        else:
            target, parent = ref, run
        if parent is not None:
            parent.remove(target)
            n += 1
    return n


def _scrub_settings(pkg: OpcPackage, report: ScrubReport) -> None:
    name = pkg.related(RT["settings"])
    if not name or name not in pkg:
        return
    root = pkg.edit(name)
    report.count(
        "document settings",
        _strip_tags(
            root, "w:rsids", "w:proofState", "w:docVars",
            "w:documentProtection", "w:writeProtection",
        ),
    )
    template = root.find(qn("w:attachedTemplate"))
    if template is not None:
        rid = template.get(qn("r:id"))
        target = pkg.rels(name).target_of(rid) if rid else None
        root.remove(template)
        report.count("attached template")
        if target and (target.startswith("\\\\") or target.lower().startswith("file:")):
            report.notes += (
                f"the attached template pointed at {target} -- recipients' Word "
                "would have stalled looking for it on open.",
            )

    # Word only offers to refresh a TOC when the document asks it to, and we
    # deliberately emit unpopulated fields rather than faking page numbers.
    update = root.find(qn("w:updateFields"))
    if update is None:
        update = etree.SubElement(root, qn("w:updateFields"))
    update.set(qn("w:val"), "true")


def _scrub_core_properties(pkg: OpcPackage, report: ScrubReport) -> None:
    name = pkg.related(RT["core"], source="")
    if not name or name not in pkg:
        return
    root = pkg.edit(name)
    cleared = 0
    found: list[str] = []
    # dc:title is the donor's report title -- "Thermal Margin Analysis of the
    # X-7 Radiator" -- and it is what Word offers as the document name, what
    # SharePoint indexes and what a PDF export writes into its metadata. It
    # belongs to the report, not to the format.
    for tag in ("dc:creator", "cp:lastModifiedBy", "cp:lastPrinted",
                "dc:title", "cp:contentStatus", "dc:identifier", "cp:version",
                "dc:description", "cp:keywords", "dc:subject", "cp:category"):
        for el in root.findall(qn(tag)):
            if el.text:
                cleared += 1
                if tag in ("dc:creator", "cp:lastModifiedBy"):
                    found.append(el.text)
            el.text = None
    revision = root.find(qn("cp:revision"))
    if revision is not None:
        revision.text = "1"
    report.count("personal properties", cleared)

    app = pkg.related(RT["extended"], source="")
    if app and app in pkg:
        app_root = pkg.edit(app)
        for tag in ("ep:Company", "ep:Manager"):
            for el in app_root.findall(qn(tag)):
                if el.text:
                    report.count("personal properties")
                    found.append(el.text)
                el.text = None
    report.identities = tuple(
        dict.fromkeys(t.strip() for t in found if len(t.strip()) > 2)
    )
