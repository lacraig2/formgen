"""Removing the donor's own content, so the template ships a format and not a report.

`scrub` takes out identity and editing state -- author names, revision ids,
spell-check caches. That is not the same job as this one. A donor is somebody's
real document, and after scrubbing it is still *their document*: their prose,
their figures, their footnotes, their client's name in a custom property, a
JPEG of page one in `docProps/thumbnail.jpeg` that no amount of reading the XML
would reveal.

The rule that separates format from data is the one the comparator already
computes:

> **The donor keeps what the corpus agreed on. What only the donor said is
> data, and goes.**

A paragraph every exemplar shares is boilerplate -- a distribution statement,
a heading, a table header -- and it is the format. A paragraph that varies
across the corpus is either a placeholder (kept as an empty control, because
its *frame* is the format) or free content (cleared, because only this author
wrote it). Headings are always kept whatever they say: they are the skeleton.

Where there is no corpus to agree -- fewer than three exemplars, so no
skeleton -- the body is left alone and the caller is told, because guessing
which sentences are format would either ship the report or gut the template.
Everything that is unambiguously data regardless of corpus size (the
thumbnail, the custom XML store, cached field results) is removed either way.

The failure mode to design against is the silent one. A template that keeps a
paragraph it should have dropped is visible the moment somebody opens it; a
template that quietly carries last quarter's client name in a custom property
is not. So this pass errs toward removing, and reports every category it
touched by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from lxml import etree

from ..oox.walk import match_key
from ..opc.ns import RT, qn
from ..opc.package import OpcPackage

# Footnotes Word itself owns: the separator rules drawn above a note block.
# They carry w:type and have no author -- removing them makes Word draw its
# own defaults, which differ by version.
_STRUCTURAL_NOTE_TYPES = {"separator", "continuationSeparator", "continuationNotice"}

# Relationship kinds that point at payload rather than at format. These are
# the only ones pruning is allowed to drop, because a dangling styles or
# numbering relationship is a repair prompt and a dangling image is not.
_PRUNABLE = ("image", "oleObject", "chart", "hyperlink", "package")

# Attributes that carry a relationship id. r:embed is the picture, r:link the
# linked-not-embedded picture, and both must be counted or pruning deletes a
# part that is still referenced.
_RID_ATTRS = ("r:id", "r:embed", "r:link", "r:pict", "r:dm", "r:lo", "r:qs", "r:cs")

# A header paragraph is format when the corpus repeats it. Below this share it
# is one author's line -- "LR-2024-0041 | L. Craig" -- and it is cleared.
HEADER_AGREEMENT = 0.5

# Whole part trees a template has no business carrying. Each is either the
# donor's own tooling rather than their format, or a store of content that no
# amount of reading word/document.xml would reveal.
#
#   glossary       Quick Parts and AutoText -- whole authored blocks
#   printerSettings a binary blob naming a printer, often a UNC path
#   vbaProject     macros: the donor's code, running on the next person's box
#   activeX        embedded controls, likewise
#   webextensions  task-pane add-ins bound to this document
#   _xmlsignatures a signature over a document we have just rewritten, so it
#                  is invalid anyway -- and it carries the signer's certificate
_DATA_TREES = (
    "word/glossary/", "word/printerSettings/", "word/webextensions/",
    "word/activeX/", "_xmlsignatures/",
)
_DATA_FILES = ("word/vbaProject.bin", "word/vbaData.xml")

# Attributes and elements describing content rather than being it. They are
# accessibility text, so they are compared against the corpus rather than
# dropped: a logo's "Acme letterhead" is format and recurs, while "site visit,
# March 2024" on the same image does not.
_DESCRIPTIONS = ("wp:docPr", "pic:cNvPr", "wps:cNvPr")
_DESCRIPTION_ATTRS = ("descr", "title", "name")


@dataclass
class RedactReport:
    cleared_blocks: int = 0
    collapsed_blocks: int = 0
    cleared_headers: int = 0
    cleared_fields: int = 0
    cleared_properties: int = 0
    dropped_notes: int = 0
    dropped_parts: tuple[str, ...] = ()
    dropped_links: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (self.cleared_blocks + self.cleared_headers + self.cleared_fields
                + self.cleared_properties + self.dropped_notes
                + len(self.dropped_parts) + self.dropped_links)

    def summary(self) -> str:
        bits: list[str] = []
        if self.cleared_blocks:
            bits.append(f"{self.cleared_blocks} paragraphs of the donor's own text")
        if self.cleared_headers:
            bits.append(f"{self.cleared_headers} header/footer lines")
        if self.cleared_fields:
            bits.append(f"{self.cleared_fields} cached field results")
        if self.cleared_properties:
            bits.append(f"{self.cleared_properties} document properties")
        if self.dropped_notes:
            bits.append(f"{self.dropped_notes} footnotes")
        if self.dropped_parts:
            bits.append(f"{len(self.dropped_parts)} parts")
        if self.dropped_links:
            bits.append(f"{self.dropped_links} links")
        if not bits:
            return "the donor carried no content of its own"
        return "removed " + ", ".join(bits)


def redact(pkg: OpcPackage, ctx=None, skeleton=None,
           corpus: dict | None = None,
           identities: tuple[str, ...] = ()) -> RedactReport:
    """Strip the donor's content, keeping what the corpus agreed is format."""
    report = RedactReport()

    # Order matters. Clearing the body first is what makes the later passes
    # able to tell an orphan from a live reference: a footnote is unreferenced
    # only once the paragraph that referenced it is gone.
    if ctx is not None and skeleton is not None:
        _clear_body(pkg, ctx, skeleton, report)
    else:
        report.notes.append(
            "the donor's body text was kept: with fewer than three exemplars "
            "there is no agreement to separate the format from one author's "
            "words. Open template.docx and delete anything that was only ever "
            "true of the document it came from."
        )

    _redact_hdrftr(pkg, corpus, report)
    _redact_unaligned_text(pkg, corpus, report)
    _unwrap_smart_tags(pkg, report)
    _clear_field_results(pkg, report)
    _clear_mail_merge(pkg, report)
    _clear_custom_properties(pkg, report)
    _clear_app_content(pkg, report)
    _drop_data_parts(pkg, report)
    _drop_unreferenced_notes(pkg, report)
    _prune_orphans(pkg, report)
    _check_identities(pkg, identities, report)
    _report_survivors(pkg, report)
    return report


# -- the body -------------------------------------------------------------


def _keep_indices(skeleton) -> set[int]:
    """Donor block indices the corpus vouches for.

    A heading is kept whatever it says -- it is the skeleton, and a template
    whose section headings were blanked is not a template. Boilerplate is kept
    because every exemplar said it. A placeholder is kept because
    materialization has already replaced its value with a prompt, so what
    survives is the frame, which is format.
    """
    from .placeholders import BOILERPLATE, PLACEHOLDER

    keep: set[int] = set()
    for slot in getattr(skeleton, "slots", ()):
        if slot.donor_index is None:
            continue
        if slot.is_heading or slot.kind in (BOILERPLATE, PLACEHOLDER):
            keep.add(slot.donor_index)
    return keep


def _clear_body(pkg: OpcPackage, ctx, skeleton, report: RedactReport) -> None:
    keep = _keep_indices(skeleton)
    pkg.touch(pkg.main_document)
    blanked: list[etree._Element] = []
    for index, features in enumerate(ctx.features):
        if index in keep or not features.is_paragraph:
            continue
        # `kind` is "table" for any paragraph inside a table, in WHICHEVER
        # part -- so a letterhead built as a table in the header reads as
        # body content here. Headers and footers are redacted by their own
        # pass, against the corpus, and blanking them from this one would
        # delete the format rather than the data.
        context = features.block.context
        if context.part != pkg.main_document:
            continue
        if context.kind not in ("body", "table"):
            continue
        if features.is_empty:
            continue
        if _blank_paragraph(features.block.element):
            blanked.append(features.block.element)
            report.cleared_blocks += 1
    report.collapsed_blocks = _collapse(blanked)


def _blank_paragraph(paragraph: etree._Element) -> bool:
    """Empty a paragraph, keeping only its properties.

    w:pPr is kept deliberately and not merely out of caution: it carries the
    style, and for the last paragraph of a section it carries that section's
    w:sectPr. Dropping it would take the page setup with it.
    """
    removed = False
    for child in list(paragraph):
        if child.tag == qn("w:pPr"):
            continue
        paragraph.remove(child)
        removed = True
    return removed


def _collapse(blanked: list[etree._Element]) -> int:
    """Reduce each run of consecutive blanked paragraphs to one.

    Three hundred empty paragraphs is not a format, it is a mess somebody has
    to clean up by hand before the template is usable. One stays, so the
    styled slot the author is meant to type into is still visible.
    """
    seen = set(map(id, blanked))
    dropped = 0
    for paragraph in blanked:
        previous = paragraph.getprevious()
        parent = paragraph.getparent()
        if parent is None or previous is None or id(previous) not in seen:
            continue
        # Never remove the paragraph carrying a section break, and never
        # empty a table cell completely -- Word repairs a w:tc with no w:p.
        props = paragraph.find(qn("w:pPr"))
        if props is not None and props.find(qn("w:sectPr")) is not None:
            continue
        if parent.tag == qn("w:tc") and len(parent.findall(qn("w:p"))) <= 1:
            continue
        parent.remove(paragraph)
        dropped += 1
    return dropped


# -- headers and footers --------------------------------------------------


def _hdrftr_parts(pkg: OpcPackage) -> list[str]:
    out: list[str] = []
    for kind in ("header", "footer"):
        for part in dict.fromkeys(pkg.related_all(RT[kind])):
            if part in pkg:
                out.append(part)
    return out


def _hdrftr_texts(pkg: OpcPackage) -> set[str]:
    keys: set[str] = set()
    for part in _hdrftr_parts(pkg):
        for paragraph in pkg.element(part).iter(qn("w:p")):
            key = match_key("".join(paragraph.itertext()))
            if key:
                keys.add(key)
    return keys


def _redact_hdrftr(pkg: OpcPackage, corpus: dict | None,
                   report: RedactReport) -> None:
    """Clear header lines the rest of the corpus does not repeat.

    Only the text goes. The runs and any fields stay exactly where they were,
    because a header's PAGE field and its tab stops are the format -- it is
    the report number typed beside them that is not.
    """
    parts = _hdrftr_parts(pkg)
    if not parts:
        return
    others = [p for name, p in sorted((corpus or {}).items()) if p is not pkg]
    if not others:
        if any(pkg.element(p).iter(qn("w:t")) for p in parts):
            report.notes.append(
                "the donor's headers and footers were kept as they were; with "
                "no other exemplar to compare against there is no way to tell "
                "a house header from one report's own title block."
            )
        return

    seen: dict[str, int] = {}
    for other in others:
        for key in _hdrftr_texts(other):
            seen[key] = seen.get(key, 0) + 1
    needed = max(1, int(len(others) * HEADER_AGREEMENT))

    for part in parts:
        root = pkg.edit(part)
        for paragraph in root.iter(qn("w:p")):
            nodes = [t for t in paragraph.iter(qn("w:t")) if (t.text or "").strip()]
            if not nodes:
                continue
            key = match_key("".join(paragraph.itertext()))
            if seen.get(key, 0) >= needed:
                continue
            for node in nodes:
                node.text = ""
            report.cleared_headers += 1


# -- fields, properties, parts -------------------------------------------


def _text_parts(pkg: OpcPackage) -> list[str]:
    parts = [pkg.main_document] + _hdrftr_parts(pkg)
    for reltype in ("footnotes", "endnotes"):
        if (part := pkg.related(RT[reltype])) and part in pkg:
            parts.append(part)
    return parts


def _clear_field_results(pkg: OpcPackage, report: RedactReport) -> None:
    """Drop what a field last evaluated to, and ask Word to work it out again.

    A DOCPROPERTY field caches its result in the runs between `separate` and
    `end`. That cache is the donor's value, and it survives every other kind
    of scrubbing because it looks like ordinary text. Marking the field dirty
    is what makes Word recompute rather than redisplay.
    """
    for part in _text_parts(pkg):
        root = pkg.edit(part)
        for paragraph in list(root.iter(qn("w:p"))):
            report.cleared_fields += _clear_fields_in(paragraph)
        # w:fldSimple holds its cached result as its own children.
        for simple in root.iter(qn("w:fldSimple")):
            if len(simple):
                for child in list(simple):
                    simple.remove(child)
                simple.set(qn("w:dirty"), "true")
                report.cleared_fields += 1


def _clear_fields_in(paragraph: etree._Element) -> int:
    """Remove the runs between `separate` and `end` at nesting depth one.

    Depth matters: a nested field's own separator would otherwise be read as
    the end of the outer field's instruction, and the outer field's real
    result would survive.
    """
    depth = 0
    showing = 0
    cleared = 0
    doomed: list[etree._Element] = []
    for run in list(paragraph.iter(qn("w:r"))):
        char = run.find(qn("w:fldChar"))
        if char is not None:
            kind = char.get(qn("w:fldCharType"))
            if kind == "begin":
                depth += 1
                if depth == 1:
                    char.set(qn("w:dirty"), "true")
            elif kind == "separate" and depth == 1:
                showing = 1
            elif kind == "end":
                if depth == 1 and showing:
                    showing = 0
                    cleared += 1
                depth = max(0, depth - 1)
            continue
        if showing and depth >= 1:
            doomed.append(run)
    for run in doomed:
        parent = run.getparent()
        if parent is not None:
            parent.remove(run)
    return cleared


def _clear_custom_properties(pkg: OpcPackage, report: RedactReport) -> None:
    """Empty custom document properties, keeping their names.

    The names are format: a DOCPROPERTY field in boilerplate refers to one by
    name, and deleting the property would leave the field with nothing to
    resolve. The values are exactly the data -- ProjectNumber, Client,
    ContractNo -- so they go.
    """
    name = pkg.related(RT["custom"], source="")
    if not name or name not in pkg:
        return
    root = pkg.edit(name)
    for prop in root.findall(qn("op:property")):
        for value in list(prop):
            if value.text and value.text.strip():
                report.cleared_properties += 1
            value.text = None


def _clear_app_content(pkg: OpcPackage, report: RedactReport) -> None:
    """Drop the parts of app.xml that summarise the document's own content.

    TitlesOfParts is a list of every heading in the document and HeadingPairs
    counts them. Both are caches Word rebuilds on save, and both leak an
    outline of a report that may never have been meant to travel.
    """
    name = pkg.related(RT["extended"], source="")
    if not name or name not in pkg:
        return
    root = pkg.edit(name)
    for tag in ("ep:TitlesOfParts", "ep:HeadingPairs", "ep:Template",
                "ep:HyperlinkBase", "ep:Words", "ep:Characters", "ep:Lines",
                "ep:Paragraphs", "ep:Pages", "ep:TotalTime"):
        for el in root.findall(qn(tag)):
            root.remove(el)
            report.cleared_properties += 1


def _drop_data_parts(pkg: OpcPackage, report: RedactReport) -> None:
    """Parts that are pure payload: the page-one thumbnail and the XML store.

    The thumbnail is the one nobody thinks of. It is a rendered picture of the
    donor's first page -- the title, the client, the report number -- stored
    as a JPEG, and it is what a file browser shows in a preview pane.
    """
    dropped = list(report.dropped_parts)
    for reltype in ("thumbnail", "customXml"):
        for source in ("", pkg.main_document):
            for part in dict.fromkeys(pkg.related_all(RT[reltype], source=source)):
                if part in pkg:
                    pkg.drop_part(part)
                    dropped.append(part)
    for reltype in ("printerSettings", "signature", "signatureOrigin"):
        for source in ("", pkg.main_document):
            for part in dict.fromkeys(pkg.related_all(RT[reltype], source=source)):
                if part in pkg:
                    pkg.drop_part(part)
                    dropped.append(part)
    # By name as well as by relationship. These stores are reached through
    # vendor relationship types that have changed more than once, and a sweep
    # that missed one because the URI moved would be a silent leak.
    for part in list(pkg.names()):
        # A .rels part is not dropped on its own -- it goes when the part it
        # belongs to goes, and asking for it by name is an error. Real
        # documents carry word/glossary/_rels/document.xml.rels, so the prefix
        # sweep walks straight into it.
        if part.endswith(".rels"):
            continue
        if part in pkg and (part.startswith("customXml/")
                            or part in _DATA_FILES
                            or part.startswith(_DATA_TREES)):
            pkg.drop_part(part)
            dropped.append(part)
    report.dropped_parts = tuple(dict.fromkeys(dropped))


def _referenced_note_ids(pkg: OpcPackage, tag: str) -> set[str]:
    ids: set[str] = set()
    for part in [pkg.main_document] + _hdrftr_parts(pkg):
        for ref in pkg.element(part).iter(qn(tag)):
            value = ref.get(qn("w:id"))
            if value is not None:
                ids.add(value)
    return ids


def _drop_unreferenced_notes(pkg: OpcPackage, report: RedactReport) -> None:
    """Remove footnotes nothing points at any more.

    Dropping the body text that referenced a footnote without dropping the
    footnote leaves the donor's prose in a part most people never open. The
    separator definitions stay: they are Word's own furniture, not content.
    """
    for reltype, tag, ref in (("footnotes", "w:footnote", "w:footnoteReference"),
                              ("endnotes", "w:endnote", "w:endnoteReference")):
        part = pkg.related(RT[reltype])
        if not part or part not in pkg:
            continue
        live = _referenced_note_ids(pkg, ref)
        root = pkg.edit(part)
        for note in list(root.findall(qn(tag))):
            kind = note.get(qn("w:type"))
            if kind in _STRUCTURAL_NOTE_TYPES:
                continue
            ident = note.get(qn("w:id")) or "0"
            if ident in live or _int_or(ident, 1) <= 0:
                continue
            root.remove(note)
            report.dropped_notes += 1


def _int_or(text: str, default: int) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


# -- orphans --------------------------------------------------------------


def _used_rids(pkg: OpcPackage, part: str) -> set[str]:
    try:
        root = pkg.element(part)
    except Exception:
        return set()
    keys = [qn(name) for name in _RID_ATTRS]
    used: set[str] = set()
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for key in keys:
            value = el.get(key)
            if value:
                used.add(value)
    return used


def _xml_parts(pkg: OpcPackage) -> Iterable[str]:
    for name in pkg.names():
        if name.endswith(".xml") and not name.endswith(".rels"):
            yield name


def _prune_orphans(pkg: OpcPackage, report: RedactReport) -> None:
    """Drop media, embeddings and links nothing refers to any longer.

    This is what carries the donor's figures and its embedded spreadsheets out
    of the package. A logo referenced from a header survives, because its
    header still refers to it -- which is the correct outcome and falls out of
    the rule rather than needing a special case.
    """
    reachable: set[str] = set()
    for part in _xml_parts(pkg):
        rels = pkg.rels(part)
        if not len(rels):
            continue
        used = _used_rids(pkg, part)
        doomed = [
            rel for rel in rels
            if rel.rid not in used and rel.reltype in {RT[k] for k in _PRUNABLE}
        ]
        if not doomed:
            continue
        for rel in doomed:
            rels.drop(rel.rid)
            if rel.external:
                report.dropped_links += 1
        pkg.touch_rels(part)

    for part in _xml_parts(pkg):
        for rel in pkg.rels(part):
            if not rel.external and (target := rel.resolve(part)):
                reachable.add(target)
    for rel in pkg.rels(""):
        if not rel.external and (target := rel.resolve("")):
            reachable.add(target)

    dropped = list(report.dropped_parts)
    for name in sorted(pkg.names()):
        if name.endswith(".rels") or name == "[Content_Types].xml":
            continue
        if name.startswith("word/media/") or name.startswith("word/embeddings/"):
            if name not in reachable:
                pkg.drop_part(name)
                dropped.append(name)
    report.dropped_parts = tuple(dict.fromkeys(dropped))


# -- the one the corpus cannot decide ------------------------------------


def _check_identities(pkg: OpcPackage, identities: tuple[str, ...],
                      report: RedactReport) -> None:
    """Warn where a kept line contains a name the scrub just removed.

    Agreement across the corpus is what tells format from data, and it fails
    in exactly one case: a corpus written by one person. Every report has
    their name in the footer, so every exemplar agrees, so consensus is
    certain it is house boilerplate. It is not -- it is their name, in every
    document anyone generates from this template from now on.

    There is no way to decide this from the corpus, so it is not decided here.
    The name is reported and left in place, because the alternative is
    deleting a line that on many real formats genuinely is the house footer.
    """
    if not identities:
        return
    hits: list[tuple[str, str]] = []
    for part in _text_parts(pkg):
        text = " ".join(pkg.element(part).itertext())
        for name in identities:
            if name in text and (name, part) not in hits:
                hits.append((name, part))
    for name, part in hits:
        where = part.rsplit("/", 1)[-1]
        report.notes.append(
            f"{name!r} was removed from the document properties but still "
            f"appears in {where}, where every exemplar agreed on it -- so it "
            "was kept as house boilerplate. If it is a person rather than the "
            "format, delete it in template.docx."
        )


# -- the regions the aligner never reaches -------------------------------
#
# The two-tier alignment covers body paragraphs and table cells. Text boxes,
# image alt text and table descriptions are outside it -- and a text-box cover
# page, which is exactly where a report's title and client tend to live,
# defeats alignment entirely. The same rule still applies, it just has to be
# asked directly: does the rest of the corpus say this too?


def _textbox_paragraphs(root: etree._Element) -> list[etree._Element]:
    out: list[etree._Element] = []
    for content in root.iter(qn("w:txbxContent")):
        out.extend(content.iter(qn("w:p")))
    return out


def _description_nodes(root: etree._Element) -> list[etree._Element]:
    out: list[etree._Element] = []
    for tag in _DESCRIPTIONS:
        try:
            out.extend(root.iter(qn(tag)))
        except ValueError:      # a prefix this package never declares
            continue
    out.extend(root.iter(qn("w:tblPr")))
    return out


def _unaligned_strings(pkg: OpcPackage) -> set[str]:
    """Everything the corpus test below compares, for one document."""
    found: set[str] = set()
    for part in _text_parts(pkg):
        try:
            root = pkg.element(part)
        except Exception:
            continue
        for paragraph in _textbox_paragraphs(root):
            if key := match_key("".join(paragraph.itertext())):
                found.add(key)
        for node in _description_nodes(root):
            for value in _description_values(node):
                found.add(match_key(value))
    return found


def _description_values(node: etree._Element) -> list[str]:
    if node.tag == qn("w:tblPr"):
        out = []
        for tag in ("w:tblCaption", "w:tblDescription"):
            child = node.find(qn(tag))
            if child is not None and (child.get(qn("w:val")) or "").strip():
                out.append(child.get(qn("w:val")))
        return out
    return [v for name in _DESCRIPTION_ATTRS
            if (v := (node.get(name) or "").strip())]


def _redact_unaligned_text(pkg: OpcPackage, corpus: dict | None,
                           report: RedactReport) -> None:
    others = [p for _, p in sorted((corpus or {}).items()) if p is not pkg]
    if not others:
        return
    seen: dict[str, int] = {}
    for other in others:
        for key in _unaligned_strings(other):
            seen[key] = seen.get(key, 0) + 1
    needed = max(1, int(len(others) * HEADER_AGREEMENT))

    def agreed(text: str) -> bool:
        return seen.get(match_key(text), 0) >= needed

    for part in _text_parts(pkg):
        root = pkg.edit(part)
        for paragraph in _textbox_paragraphs(root):
            text = "".join(paragraph.itertext())
            if not text.strip() or agreed(text):
                continue
            if _blank_paragraph(paragraph):
                report.cleared_blocks += 1
        for node in _description_nodes(root):
            if node.tag == qn("w:tblPr"):
                for tag in ("w:tblCaption", "w:tblDescription"):
                    child = node.find(qn(tag))
                    if child is None:
                        continue
                    if not agreed(child.get(qn("w:val")) or ""):
                        node.remove(child)
                        report.cleared_properties += 1
                continue
            for name in _DESCRIPTION_ATTRS:
                value = (node.get(name) or "").strip()
                # @name is a shape's own label ("Picture 1"); it only carries
                # anything when somebody renamed it, and then it is a caption.
                if value and not agreed(value):
                    del node.attrib[name]
                    report.cleared_properties += 1


def _unwrap_smart_tags(pkg: OpcPackage, report: RedactReport) -> None:
    """Take the smart-tag wrappers off, keeping their runs.

    A w:smartTag is a transparent wrapper Word 2003 put around text it thought
    it recognised -- a person, a place, a stock ticker -- and w:smartTagPr/w:attr
    holds what it decided. Unwrapping keeps every visible run and loses only
    the annotation.
    """
    for part in _text_parts(pkg):
        root = pkg.edit(part)
        for tag in list(root.iter(qn("w:smartTag"))):
            parent = tag.getparent()
            if parent is None:
                continue
            index = list(parent).index(tag)
            props = tag.find(qn("w:smartTagPr"))
            if props is not None:
                tag.remove(props)
            for offset, child in enumerate(list(tag)):
                parent.insert(index + offset, child)
            parent.remove(tag)
            report.cleared_properties += 1


def _clear_mail_merge(pkg: OpcPackage, report: RedactReport) -> None:
    """Remove the merge setup, which names a recipient list and where it lives.

    w:odso carries the connection string -- a path under someone's profile, or
    a server -- and w:query the SQL. A template that opens asking for a
    spreadsheet nobody has is also just broken.
    """
    name = pkg.related(RT["settings"])
    if name and name in pkg:
        root = pkg.edit(name)
        for element in list(root.iter(qn("w:mailMerge"))):
            parent = element.getparent()
            if parent is not None:
                parent.remove(element)
                report.cleared_properties += 1
    dropped = list(report.dropped_parts)
    for reltype in ("mailMergeSource", "mailMergeHeaderSource"):
        for source in ("", pkg.main_document):
            rels = pkg.rels(source)
            for rel in list(rels):
                if rel.reltype != RT[reltype]:
                    continue
                target = None if rel.external else rel.resolve(source)
                rels.drop(rel.rid)
                pkg.touch_rels(source)
                report.dropped_links += 1
                if target and target in pkg:
                    pkg.drop_part(target)
                    dropped.append(target)
    report.dropped_parts = tuple(dict.fromkeys(dropped))


# -- what survived, and what we are not going to decide -------------------

# Bytes that mean a picture is carrying metadata of its own. Stripping it
# would mean re-encoding somebody's logo, so this reports rather than acts.
_IMAGE_METADATA = (
    (b"Exif\x00\x00", "EXIF"),
    (b"http://ns.adobe.com/xap/", "XMP"),
    (b"photoshop:", "Photoshop"),
    (b"GPS", "GPS"),
)


def _report_survivors(pkg: OpcPackage, report: RedactReport) -> None:
    """Name what was kept on purpose but a person might still want gone.

    Two kinds. A sensitivity label and a ribbon customisation are an
    organisation's policy, and quietly removing an organisation's protection
    label from a document is not a decision a formatting tool gets to make.
    Image metadata is the other: stripping it means re-encoding the image, and
    re-encoding somebody's letterhead to remove a GPS tag it probably does not
    have is the worse trade.
    """
    for part, what in (("docMetadata/LabelInfo.xml", "a sensitivity label"),
                       ("customUI/customUI.xml", "a ribbon customisation"),
                       ("customUI/customUI14.xml", "a ribbon customisation")):
        if part in pkg:
            report.notes.append(
                f"template.docx still carries {what} ({part}) from the donor. "
                "It was kept because removing it is your organisation's "
                "decision, not this tool's -- delete the part if it should "
                "not travel with the format."
            )
    for name in sorted(pkg.names()):
        if not name.startswith("word/media/"):
            continue
        blob = pkg.blob(name)
        kinds = sorted({label for marker, label in _IMAGE_METADATA
                        if marker in blob})
        if kinds:
            report.notes.append(
                f"{name} is still referenced by the format and carries "
                f"{', '.join(kinds)} metadata. Removing it would mean "
                "re-encoding the image, so it was left alone -- check it if "
                "the picture was taken rather than drawn."
            )
