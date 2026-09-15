"""Carrying the format into the document, rather than the document into a template.

Reformatting joins two packages: the profile holds the *format* parts
(styles, theme, numbering, section setup, headers and footers), the document
holds the *content* parts (the body, footnotes, comments, media, embeddings).
Either can be carried into the other, and the choice is the most consequential
one in the codebase.

**We carry the format in.** Under the graft, every `r:id` in the body is
already valid, footnote ids already match `footnotes.xml`, comment anchors
already match their four sidecar parts, OLE objects still point at their
embeddings and bookmark pairs are intact -- none of that is touched, so none
of it can be lost. The only relationship plumbing is the donor's own headers
and footers and the images they carry: a bounded set we control and test once.

The other direction would require re-homing every `a:blip/@r:embed` and its
SVG companion, the VML fallback's `v:imagedata/@r:id` consistently to the same
media part, `w:hyperlink/@r:id` with its TargetMode, OLE relationships and
previews, chart subtrees with their embedded workbooks; minting collision-free
part names; registering content types; renumbering `wp:docPr/@id` document
wide; renumbering footnote ids against the donor's separators; and merging four
comment parts. Anything missed there is silent data loss in someone's document.

> The graft's failure mode is *residue* -- a stray `w:shd` survives. Visible,
> lintable, fixable next run. The rebuild's failure mode is *deletion* --
> invisible and unrecoverable. For a tool that rewrites other people's
> documents, choose the strategy whose worst case is "not clean enough".

Copied parts keep the relationship ids their own XML already refers to, so no
copied XML is ever rewritten -- only the `.rels` targets are re-pointed.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from ..opc.rels import Relationships, encode_target

# Format parts replaced wholesale. Each is the format and nothing but.
REPLACED = ("styles", "theme", "fontTable")

# settings.xml is NOT replaced wholesale: it mixes format (how headers are
# organised, tab stops, compatibility mode) with document state (revision save
# ids, document variables, footnote numbering the author chose). Only the
# format half is carried.
CARRIED_SETTINGS = (
    "w:evenAndOddHeaders",   # document-wide, and headers do not render without it
    "w:defaultTabStop",
    "w:characterSpacingControl",
    "w:compat",
    "w:themeFontLang",
    "w:mirrorMargins",
    "w:gutterAtTop",
)

# CT_Settings is a SEQUENCE, so a settings.xml whose children are in the wrong
# order is a file Word offers to repair. This is the subset we insert or move,
# plus enough real-world neighbours to place them correctly; anything not
# listed keeps the position it already had, because we never move what we did
# not come for.
_SETTINGS_ORDER = (
    "w:writeProtection", "w:view", "w:zoom", "w:removePersonalInformation",
    "w:removeDateAndTime", "w:displayBackgroundShape", "w:embedTrueTypeFonts",
    "w:saveSubsetFonts", "w:mirrorMargins", "w:bordersDoNotSurroundHeader",
    "w:bordersDoNotSurroundFooter", "w:gutterAtTop", "w:hideSpellingErrors",
    "w:hideGrammaticalErrors", "w:activeWritingStyle", "w:proofState",
    "w:attachedTemplate", "w:linkStyles", "w:stylePaneFormatFilter",
    "w:documentType", "w:mailMerge", "w:revisionView", "w:trackChanges",
    "w:doNotTrackMoves", "w:doNotTrackFormatting", "w:documentProtection",
    "w:autoFormatOverride", "w:styleLockTheme", "w:styleLockQFSet",
    "w:defaultTabStop", "w:autoHyphenation", "w:consecutiveHyphenLimit",
    "w:hyphenationZone", "w:doNotHyphenateCaps", "w:summaryLength",
    "w:clickAndTypeStyle", "w:defaultTableStyle", "w:evenAndOddHeaders",
    "w:bookFoldRevPrinting", "w:bookFoldPrinting", "w:bookFoldPrintingSheets",
    "w:drawingGridHorizontalSpacing", "w:drawingGridVerticalSpacing",
    "w:displayHorizontalDrawingGridEvery", "w:displayVerticalDrawingGridEvery",
    "w:doNotUseMarginsForDrawingGridOrigin", "w:characterSpacingControl",
    "w:printTwoOnOne", "w:strictFirstAndLastChars", "w:noLineBreaksAfter",
    "w:noLineBreaksBefore", "w:savePreviewPicture",
    "w:doNotValidateAgainstSchema", "w:saveInvalidXml", "w:ignoreMixedContent",
    "w:alwaysShowPlaceholderText", "w:doNotDemarcateInvalidXml",
    "w:saveXmlDataOnly", "w:useXSLTWhenSaving", "w:showXMLTags",
    "w:alwaysMergeEmptyNamespace", "w:updateFields", "w:hdrShapeDefaults",
    "w:footnotePr", "w:endnotePr", "w:compat", "w:docVars", "w:rsids",
    "m:mathPr", "w:attachedSchema", "w:themeFontLang", "w:clrSchemeMapping",
    "w:doNotIncludeSubdocsInStats", "w:doNotAutoCompressPictures",
    "w:forceUpgrade", "w:captions", "w:readModeInkLockDown", "w:smartTagType",
    "w:shapeDefaults", "w:doNotEmbedSmartTags", "w:decimalSymbol",
    "w:listSeparator",
)

# Section properties that ARE the page setup.
PAGE_PROPERTIES = ("w:pgSz", "w:pgMar", "w:cols", "w:docGrid", "w:pgBorders")

# CT_SectPr child order. Writing these out of order produces a file Word
# offers to repair, and the references must come first.
_SECTPR_ORDER = (
    "w:headerReference", "w:footerReference", "w:footnotePr", "w:endnotePr",
    "w:type", "w:pgSz", "w:pgMar", "w:paperSrc", "w:pgBorders", "w:lnNumType",
    "w:pgNumType", "w:cols", "w:formProt", "w:vAlign", "w:noEndnote",
    "w:titlePg", "w:textDirection", "w:bidi", "w:rtlGutter", "w:docGrid",
    "w:printerSettings", "w:sectPrChange",
)


@dataclass
class GraftReport:
    replaced: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    sections: int = 0
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = []
        if self.replaced:
            bits.append(f"{len(self.replaced)} format part(s) replaced")
        if self.added:
            bits.append(f"{len(self.added)} part(s) grafted")
        if self.sections:
            bits.append(f"{self.sections} section(s) re-set")
        return ", ".join(bits) or "nothing to graft"


def graft(
    target: OpcPackage,
    donor: OpcPackage,
    numbering_root: etree._Element | None = None,
    headers: bool = True,
) -> GraftReport:
    """Carry the donor's format parts onto `target`, in place."""
    report = GraftReport()
    main = target.main_document

    for reltype in REPLACED:
        _replace_part(target, donor, reltype, report)
    if numbering_root is not None:
        _install_numbering(target, numbering_root, report)
    _carry_settings(target, donor, report)

    donor_sect = _donor_sectpr(donor)
    if donor_sect is not None:
        mapping = _graft_hdrftr(target, donor, donor_sect, report) if headers else {}
        report.sections = _apply_page_setup(target, main, donor_sect, mapping)
    return report


# -- whole parts ----------------------------------------------------------


def _replace_part(
    target: OpcPackage, donor: OpcPackage, reltype: str, report: GraftReport
) -> None:
    source = donor.related(RT[reltype])
    if not source or source not in donor:
        return
    blob = donor.blob(source)
    existing = target.related(RT[reltype])
    if existing and existing in target:
        target.replace_part(existing, blob)
        report.replaced.append(existing)
        return
    name = target.unique_partname(
        posixpath.join(posixpath.dirname(target.main_document),
                       posixpath.basename(source)).replace("{", "{")
    )
    content_type = donor.content_types.for_part(source)
    target.add_part(name, blob, content_type)
    target.relate(RT[reltype], name, target.main_document)
    report.added.append(name)


def _install_numbering(
    target: OpcPackage, root: etree._Element, report: GraftReport
) -> None:
    blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                          standalone=True)
    existing = target.related(RT["numbering"])
    if existing and existing in target:
        target.replace_part(existing, blob)
        report.replaced.append(existing)
        return
    name = posixpath.join(posixpath.dirname(target.main_document), "numbering.xml")
    target.add_part(name, blob, target.content_types.for_part(name)
                    or "application/vnd.openxmlformats-officedocument."
                       "wordprocessingml.numbering+xml")
    target.relate(RT["numbering"], name, target.main_document)
    report.added.append(name)


def _carry_settings(
    target: OpcPackage, donor: OpcPackage, report: GraftReport
) -> None:
    """Carry only the format half of settings.xml.

    Replacing it wholesale would take the donor's revision-save ids, document
    variables and footnote numbering with it -- state that belongs to the
    document being reformatted, not to the format.
    """
    donor_part = donor.related(RT["settings"])
    target_part = target.related(RT["settings"])
    if not donor_part or donor_part not in donor:
        return
    donor_root = donor.element(donor_part)
    if not target_part or target_part not in target:
        return
    root = target.edit(target_part)
    for tag in CARRIED_SETTINGS:
        wanted = donor_root.findall(qn(tag))
        existing = root.findall(qn(tag))
        # Replace in place where the element is already there, so nothing
        # that was in a legal position is moved out of one.
        for old, new in zip(existing, wanted):
            old.addprevious(_deepcopy(new))
            root.remove(old)
        for old in existing[len(wanted):]:
            root.remove(old)
        for new in wanted[len(existing):]:
            _place(root, _deepcopy(new))
    # Fields we emit are deliberately unpopulated -- faking page numbers is
    # worse than omitting them -- so the document must ask Word to update.
    update = root.find(qn("w:updateFields"))
    if update is None:
        update = etree.Element(qn("w:updateFields"))
        _place(root, update)
    update.set(qn("w:val"), "true")
    report.replaced.append(f"{target_part} (format settings only)")


def _place(root: etree._Element, element: etree._Element) -> None:
    """Insert at the position CT_Settings requires, disturbing nothing else."""
    order = {qn(tag): i for i, tag in enumerate(_SETTINGS_ORDER)}
    rank = order.get(element.tag)
    if rank is None:
        root.append(element)
        return
    for child in root:
        if not isinstance(child.tag, str):
            continue
        other = order.get(child.tag)
        if other is not None and other > rank:
            child.addprevious(element)
            return
    root.append(element)


def _deepcopy(element: etree._Element) -> etree._Element:
    import copy

    return copy.deepcopy(element)


# -- headers, footers, and the media they carry ---------------------------


def _donor_sectpr(donor: OpcPackage) -> etree._Element | None:
    body = donor.element(donor.main_document).find(qn("w:body"))
    if body is None:
        return None
    sections = [c for c in body if isinstance(c.tag, str) and c.tag == qn("w:sectPr")]
    return sections[-1] if sections else None


def _graft_hdrftr(
    target: OpcPackage, donor: OpcPackage, donor_sect: etree._Element,
    report: GraftReport,
) -> dict[str, tuple[str, str]]:
    """Copy the donor's header/footer parts across. Returns rId -> (type, kind)."""
    mapping: dict[str, tuple[str, str]] = {}
    copied: dict[str, str] = {}
    for tag, kind in ((qn("w:headerReference"), "header"),
                      (qn("w:footerReference"), "footer")):
        for reference in donor_sect.findall(tag):
            rid = reference.get(qn("r:id"))
            if not rid:
                continue
            rel = donor.rels(donor.main_document).get(rid)
            source = rel.resolve(donor.main_document) if rel else None
            source = donor.actual_name(source) if source else None
            if not source:
                continue
            if source not in copied:
                copied[source] = copy_part(
                    donor, target, source,
                    posixpath.join(posixpath.dirname(target.main_document),
                                   kind + "{n}.xml"),
                    report,
                )
            new_rid = target.relate(RT[kind], copied[source], target.main_document)
            mapping[new_rid] = (reference.get(qn("w:type")) or "default", kind)
    return mapping


def copy_part(
    donor: OpcPackage, target: OpcPackage, source: str, template: str,
    report: GraftReport, seen: dict[str, str] | None = None,
) -> str:
    """Copy a part and everything it depends on, keeping its own rIds.

    Because the copy keeps the relationship ids its XML already refers to,
    no copied XML is rewritten -- only the new `.rels` part's targets. Not
    rewriting a customer's XML is always the safer of the two options, and
    here it is also the simpler one.
    """
    seen = seen if seen is not None else {}
    if source in seen:
        return seen[source]

    name = target.unique_partname(template)
    target.add_part(name, donor.blob(source), donor.content_types.for_part(source))
    seen[source] = name
    report.added.append(name)

    donor_rels = donor.rels(source)
    if not len(donor_rels):
        return name
    rels: Relationships = target.touch_rels(name)
    for rel in donor_rels:
        if rel.external:
            rels.put(rel.rid, rel.reltype, rel.target, external=True)
            continue
        child = donor.actual_name(rel.resolve(source) or "")
        if not child:
            report.notes.append(
                f"{source}: relationship {rel.rid} resolved nowhere in the "
                "profile and was not carried over"
            )
            continue
        child_template = posixpath.join(
            posixpath.dirname(child),
            _template_for(posixpath.basename(child)),
        )
        copied = copy_part(donor, target, child, child_template, report, seen)
        rels.put(rel.rid, rel.reltype, encode_target(copied, name))
    return name


def _template_for(basename: str) -> str:
    stem, dot, extension = basename.rpartition(".")
    stem = stem or basename
    stem = stem.rstrip("0123456789") or stem
    return f"{stem}{{n}}{dot}{extension}" if dot else f"{stem}{{n}}"


# -- page setup -----------------------------------------------------------


def _apply_page_setup(
    target: OpcPackage, main: str, donor_sect: etree._Element,
    hdrftr: dict[str, tuple[str, str]],
) -> int:
    """Re-set every section of the target to the donor's page setup.

    The target's *section structure* is preserved -- a document with a
    landscape annex keeps having one -- but each section's geometry, header
    and footer references come from the donor. An absent reference would mean
    "same as the previous section", so every section gets explicit ones.
    """
    root = target.edit(main)
    body = root.find(qn("w:body"))
    if body is None:
        return 0
    sections = list(body.findall(f".//{qn('w:sectPr')}"))
    for sect in sections:
        for tag in PAGE_PROPERTIES:
            for existing in sect.findall(qn(tag)):
                sect.remove(existing)
            for wanted in donor_sect.findall(qn(tag)):
                sect.append(_deepcopy(wanted))
        for tag in (qn("w:headerReference"), qn("w:footerReference")):
            for existing in sect.findall(tag):
                sect.remove(existing)
        for rid, (slot, kind) in sorted(hdrftr.items()):
            reference = etree.SubElement(sect, qn(f"w:{kind}Reference"))
            reference.set(qn("w:type"), slot)
            reference.set(qn("r:id"), rid)
        for tag in ("w:titlePg",):
            for existing in sect.findall(qn(tag)):
                sect.remove(existing)
            for wanted in donor_sect.findall(qn(tag)):
                sect.append(_deepcopy(wanted))
        _order_sectpr(sect)
    return len(sections)


def _order_sectpr(sect: etree._Element) -> None:
    index = {qn(tag): i for i, tag in enumerate(_SECTPR_ORDER)}
    for child in sorted(sect, key=lambda c: index.get(c.tag, len(index))):
        sect.append(child)
