"""settings.xml -- the document-wide switches, several of which are load-bearing.

Most of settings.xml is noise we copy through untouched. A handful of elements
are not:

* **w:evenAndOddHeaders is document-wide** while w:titlePg is per-section.
  Together they decide which of a section's three header slots Word actually
  renders, so neither can be interpreted without the other (see sections.py).
* **w:documentProtection and w:trackChanges are refusal conditions.**
  Restyling a document with revision records turns those records into lies,
  and enforced protection means our output would not open the way the author
  intended. Both must be detected before anything is written.
* **w:updateFields** is how a generated TOC gets populated without us
  pretending to paginate.
* **w:attachedTemplate** pointing at a UNC path is inert for us but hangs the
  recipient's Word on open, so the donor scrub removes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import qn
from .values import Length


def _el(root: etree._Element, tag: str) -> etree._Element | None:
    return root.find(qn(tag))


def _flag(root: etree._Element, tag: str) -> bool:
    el = _el(root, tag)
    if el is None:
        return False
    return (el.get(qn("w:val")) or "1").strip().lower() not in ("0", "false", "off")


@dataclass(frozen=True)
class DocumentProtection:
    """w:documentProtection. Only `enforced` actually stops the user."""

    edit: str | None = None          # readOnly|comments|trackedChanges|forms
    enforced: bool = False
    has_password: bool = False

    @property
    def blocks_editing(self) -> bool:
        return self.enforced and self.edit in ("readOnly", "comments", "forms")


@dataclass
class Settings:
    """The subset of settings.xml that changes what we may do."""

    even_and_odd_headers: bool = False
    mirror_margins: bool = False
    gutter_at_top: bool = False
    track_changes: bool = False
    update_fields: bool = False
    protection: DocumentProtection = field(default_factory=DocumentProtection)
    write_protection: bool = False
    default_tab_stop: Length | None = None
    attached_template_rid: str | None = None
    compat_mode: int | None = None
    proof_state: bool = False        # a stale spell/grammar cache is scrub-worthy
    has_rsids: bool = False
    doc_vars: tuple[str, ...] = ()
    element: etree._Element | None = None

    @classmethod
    def parse(cls, root: etree._Element | None) -> Settings:
        if root is None:
            return cls()
        protection = DocumentProtection()
        if (prot := _el(root, "w:documentProtection")) is not None:
            enforcement = (prot.get(qn("w:enforcement")) or "").strip().lower()
            protection = DocumentProtection(
                edit=prot.get(qn("w:edit")),
                enforced=enforcement in ("1", "true", "on"),
                has_password=any(
                    prot.get(qn(a)) for a in ("w:hash", "w:salt", "w:cryptProviderType")
                ),
            )
        compat_mode = None
        if (compat := _el(root, "w:compat")) is not None:
            for setting in compat.findall(qn("w:compatSetting")):
                if setting.get(qn("w:name")) == "compatibilityMode":
                    raw = setting.get(qn("w:val"))
                    if raw and raw.isdigit():
                        compat_mode = int(raw)
        tab = _el(root, "w:defaultTabStop")
        template = _el(root, "w:attachedTemplate")
        doc_vars = _el(root, "w:docVars")
        return cls(
            even_and_odd_headers=_flag(root, "w:evenAndOddHeaders"),
            mirror_margins=_flag(root, "w:mirrorMargins"),
            gutter_at_top=_flag(root, "w:gutterAtTop"),
            track_changes=_flag(root, "w:trackChanges"),
            update_fields=_flag(root, "w:updateFields"),
            protection=protection,
            write_protection=_el(root, "w:writeProtection") is not None,
            default_tab_stop=Length.parse(
                tab.get(qn("w:val")) if tab is not None else None
            ),
            attached_template_rid=(
                template.get(qn("r:id")) if template is not None else None
            ),
            compat_mode=compat_mode,
            proof_state=_el(root, "w:proofState") is not None,
            has_rsids=_el(root, "w:rsids") is not None,
            doc_vars=tuple(
                name for v in (doc_vars.findall(qn("w:docVar")) if doc_vars is not None else [])
                if (name := v.get(qn("w:name")))
            ),
            element=root,
        )

    @property
    def refusal_reasons(self) -> list[str]:
        """Conditions under which we must not rewrite this document.

        Each names the remedy, because a bare refusal with no way forward is
        the thing that makes people work around the tool.
        """
        out: list[str] = []
        if self.track_changes:
            out.append(
                "track changes is turned on; restyling would attribute our edits "
                "to the author. Turn it off (Review > Track Changes) and re-run."
            )
        if self.protection.enforced:
            out.append(
                f"document protection is enforced (edit={self.protection.edit!r}); "
                "remove it under Review > Restrict Editing > Stop Protection."
            )
        if self.write_protection:
            out.append(
                "the document is write-protected; save an unprotected copy "
                "(File > Save As) and run against that."
            )
        return out
