"""Sections: page setup, and which header or footer a page actually gets.

Two rules make this module necessary rather than a twenty-line attribute read.

**Where a section lives.** A w:sectPr describes the section that *ends* at it.
The final section's properties are the last child of w:body; every other
section's live in the w:pPr of that section's last paragraph. So sections are
discovered by walking the body in order, not by a findall -- and appending a
w:p after the body-level w:sectPr corrupts the file, which is why
:meth:`SectionModel.problems` checks for it.

**Absence of a w:headerReference means "link to previous", not "no header".**
This is the single most common way a generated document goes wrong: a cover
page that omits the header inherits the body's header rather than showing
none. Suppressing a header genuinely requires an explicitly empty header part.
Inheritance is per type and per kind -- Word lets you unlink the first-page
header while the default stays linked -- so each of the six slots is resolved
independently.

Which of the three slots is live depends on settings from two different
places: w:titlePg is per-section, while w:evenAndOddHeaders is document-wide
in settings.xml. With evenAndOddHeaders off, every w:type="even" reference in
the file is dead weight -- real, resolvable, and never rendered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from .settings import Settings
from .values import Length

# Resolution order matters nowhere, but this order matches Word's UI.
HDRFTR_TYPES: tuple[str, ...] = ("first", "even", "default")

# Wrappers a section-terminating paragraph can legitimately sit inside. Kept
# in step with walk.py: a sectPr inside a tracked insertion is rare but real,
# and missing it merges two sections into one.
_TRANSPARENT = {
    qn("w:customXml"), qn("w:ins"), qn("w:del"),
    qn("w:moveTo"), qn("w:moveFrom"),
}

_BLOCK_TAGS = {qn("w:p"), qn("w:tbl"), qn("m:oMathPara"), qn("w:altChunk")}


def _attr_len(el: etree._Element | None, name: str) -> Length | None:
    return Length.parse(el.get(qn(name))) if el is not None else None


def _attr_int(el: etree._Element | None, name: str) -> int | None:
    if el is None:
        return None
    raw = el.get(qn(name))
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _attr_on(el: etree._Element | None, name: str, default: bool = False) -> bool:
    if el is None:
        return default
    raw = el.get(qn(name))
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "on")


def _val(parent: etree._Element, tag: str) -> str | None:
    el = parent.find(qn(tag))
    return el.get(qn("w:val")) if el is not None else None


def _flag(parent: etree._Element, tag: str) -> bool:
    el = parent.find(qn(tag))
    if el is None:
        return False
    return (el.get(qn("w:val")) or "1").strip().lower() not in ("0", "false", "off")


@dataclass(frozen=True)
class PageSize:
    width: Length | None = None
    height: Length | None = None
    orient_attr: str | None = None

    @property
    def orientation(self) -> str:
        """Explicit w:orient if given, else inferred from the dimensions.

        Word writes orient="landscape" *and* swaps w/h, so the two agree in
        well-formed files. Producers that swap without writing the attribute
        are common enough that inferring is the safer default.
        """
        if self.orient_attr in ("portrait", "landscape"):
            return self.orient_attr
        if self.width and self.height and self.width.twips > self.height.twips:
            return "landscape"
        return "portrait"

    @classmethod
    def parse(cls, el: etree._Element | None) -> PageSize:
        return cls(
            width=_attr_len(el, "w:w"),
            height=_attr_len(el, "w:h"),
            orient_attr=el.get(qn("w:orient")) if el is not None else None,
        )


@dataclass(frozen=True)
class Margins:
    top: Length | None = None
    right: Length | None = None
    bottom: Length | None = None
    left: Length | None = None
    header: Length | None = None    # distance from page edge to header
    footer: Length | None = None
    gutter: Length | None = None

    @classmethod
    def parse(cls, el: etree._Element | None) -> Margins:
        return cls(
            # w:top and w:bottom are SIGNED: a negative bottom margin means the
            # footer may overflow it. Length.parse keeps the sign.
            top=_attr_len(el, "w:top"),
            right=_attr_len(el, "w:right"),
            bottom=_attr_len(el, "w:bottom"),
            left=_attr_len(el, "w:left"),
            header=_attr_len(el, "w:header"),
            footer=_attr_len(el, "w:footer"),
            gutter=_attr_len(el, "w:gutter"),
        )


@dataclass(frozen=True)
class Columns:
    count: int = 1
    space: Length | None = None
    equal_width: bool = True
    separator: bool = False
    widths: tuple[Length, ...] = ()

    @classmethod
    def parse(cls, el: etree._Element | None) -> Columns:
        if el is None:
            return cls()
        cols = el.findall(qn("w:col"))
        return cls(
            count=_attr_int(el, "w:num") or (len(cols) or 1),
            space=_attr_len(el, "w:space"),
            # ST_OnOff attribute whose schema default is true.
            equal_width=_attr_on(el, "w:equalWidth", default=True),
            separator=_attr_on(el, "w:sep"),
            widths=tuple(
                w for c in cols if (w := _attr_len(c, "w:w")) is not None
            ),
        )


@dataclass(frozen=True)
class PageNumbering:
    start: int | None = None
    fmt: str | None = None            # decimal | lowerRoman | upperRoman | ...
    chapter_style: int | None = None
    chapter_separator: str | None = None

    @classmethod
    def parse(cls, el: etree._Element | None) -> PageNumbering:
        if el is None:
            return cls()
        return cls(
            start=_attr_int(el, "w:start"),
            fmt=el.get(qn("w:fmt")),
            chapter_style=_attr_int(el, "w:chapStyle"),
            chapter_separator=el.get(qn("w:chapSep")),
        )


@dataclass(frozen=True)
class LineNumbering:
    count_by: int | None = None
    start: int | None = None
    restart: str | None = None        # newPage | newSection | continuous
    distance: Length | None = None

    @classmethod
    def parse(cls, el: etree._Element | None) -> LineNumbering | None:
        if el is None:
            return None
        return cls(
            count_by=_attr_int(el, "w:countBy"),
            start=_attr_int(el, "w:start"),
            restart=el.get(qn("w:restart")),
            distance=_attr_len(el, "w:distance"),
        )


@dataclass
class Section:
    """One w:sectPr and where it was found."""

    index: int
    element: etree._Element
    owner_paragraph: etree._Element | None   # None for the final, body-level one
    page: PageSize = field(default_factory=PageSize)
    margins: Margins = field(default_factory=Margins)
    columns: Columns = field(default_factory=Columns)
    page_numbering: PageNumbering = field(default_factory=PageNumbering)
    line_numbering: LineNumbering | None = None
    break_type: str = "nextPage"      # nextPage | continuous | evenPage | oddPage | nextColumn
    title_page: bool = False
    vertical_align: str | None = None
    text_direction: str | None = None
    rtl_gutter: bool = False
    form_protection: bool = False
    has_page_borders: bool = False
    has_doc_grid: bool = False
    header_rids: dict[str, str] = field(default_factory=dict)
    footer_rids: dict[str, str] = field(default_factory=dict)

    @property
    def is_final(self) -> bool:
        return self.owner_paragraph is None

    @classmethod
    def parse(
        cls, el: etree._Element, index: int, owner: etree._Element | None
    ) -> Section:
        header_rids: dict[str, str] = {}
        footer_rids: dict[str, str] = {}
        for tag, sink in ((qn("w:headerReference"), header_rids),
                          (qn("w:footerReference"), footer_rids)):
            for ref in el.findall(tag):
                rid = ref.get(qn("r:id"))
                if not rid:
                    continue
                # An omitted w:type means "default" per 17.6.12.
                sink[ref.get(qn("w:type")) or "default"] = rid
        return cls(
            index=index,
            element=el,
            owner_paragraph=owner,
            page=PageSize.parse(el.find(qn("w:pgSz"))),
            margins=Margins.parse(el.find(qn("w:pgMar"))),
            columns=Columns.parse(el.find(qn("w:cols"))),
            page_numbering=PageNumbering.parse(el.find(qn("w:pgNumType"))),
            line_numbering=LineNumbering.parse(el.find(qn("w:lnNumType"))),
            break_type=_val(el, "w:type") or "nextPage",
            title_page=_flag(el, "w:titlePg"),
            vertical_align=_val(el, "w:vAlign"),
            text_direction=_val(el, "w:textDirection"),
            rtl_gutter=_flag(el, "w:rtlGutter"),
            form_protection=_flag(el, "w:formProt"),
            has_page_borders=el.find(qn("w:pgBorders")) is not None,
            has_doc_grid=el.find(qn("w:docGrid")) is not None,
            header_rids=header_rids,
            footer_rids=footer_rids,
        )

    # -- derived geometry ----------------------------------------------

    @property
    def content_width(self) -> Length | None:
        """Text width: page width less both margins and the gutter.

        This is what an image must be scaled to fit, and what a table's
        preferred width in fiftieths of a percent is a percentage *of*.
        """
        if not self.page.width:
            return None
        used = sum(
            m.twips for m in (self.margins.left, self.margins.right, self.margins.gutter)
            if m is not None
        )
        return Length(self.page.width.twips - used)

    @property
    def content_height(self) -> Length | None:
        if not self.page.height:
            return None
        used = sum(
            m.twips for m in (self.margins.top, self.margins.bottom) if m is not None
        )
        return Length(self.page.height.twips - used)

    @property
    def column_width(self) -> Length | None:
        """Width of a single column, for equal-width multi-column sections."""
        width = self.content_width
        if width is None or self.columns.count <= 1:
            return width
        if not self.columns.equal_width and self.columns.widths:
            return self.columns.widths[0]
        gaps = (self.columns.count - 1) * (
            self.columns.space.twips if self.columns.space else 0
        )
        return Length((width.twips - gaps) // self.columns.count)

    def signature(self) -> tuple:
        """Cross-document page-setup key, free of rIds and part names."""
        def tw(length: Length | None) -> int | None:
            return length.twips if length else None

        return (
            self.page.orientation,
            tw(self.page.width), tw(self.page.height),
            tw(self.margins.top), tw(self.margins.right),
            tw(self.margins.bottom), tw(self.margins.left),
            tw(self.margins.header), tw(self.margins.footer), tw(self.margins.gutter),
            self.columns.count,
            self.break_type,
            self.title_page,
            self.page_numbering.fmt,
            self.page_numbering.start,
        )


@dataclass(frozen=True)
class HdrFtrRef:
    """A resolved header or footer slot."""

    kind: str                 # "header" | "footer"
    type: str                 # "first" | "even" | "default"
    part: str | None          # None when nothing is inherited -- Word draws blank
    defined_in: int | None    # the section that actually declared it
    inherited: bool           # True when this section is "linked to previous"
    active: bool              # False when titlePg/evenAndOddHeaders make it dead


class SectionModel:
    """Every section of a document, with header/footer inheritance resolved."""

    def __init__(
        self,
        sections: list[Section],
        settings: Settings | None = None,
        pkg: OpcPackage | None = None,
        part: str | None = None,
        trailing_blocks: int = 0,
    ):
        self.sections = sections
        self.settings = settings or Settings()
        self.pkg = pkg
        self.part = part
        # Blocks after the body-level sectPr. Must be zero: Word treats a
        # paragraph there as corruption and offers to repair the file.
        self.trailing_blocks = trailing_blocks
        self._by_element: dict[etree._Element, int] = {}

    # -- construction ---------------------------------------------------

    @classmethod
    def from_package(cls, pkg: OpcPackage, settings: Settings | None = None) -> SectionModel:
        part = pkg.main_document
        body = pkg.element(part).find(qn("w:body"))
        if settings is None:
            sname = pkg.related(RT["settings"])
            settings = Settings.parse(
                pkg.element(sname) if sname and sname in pkg else None
            )
        model = cls.from_body(body, settings)
        model.pkg, model.part = pkg, part
        return model

    @classmethod
    def from_body(
        cls, body: etree._Element | None, settings: Settings | None = None
    ) -> SectionModel:
        sections: list[Section] = []
        owners: dict[etree._Element, int] = {}
        trailing = 0
        if body is None:
            return cls(sections, settings)

        index = 0
        final_sect = None
        for child in body:
            if not isinstance(child.tag, str):
                continue
            if child.tag == qn("w:sectPr"):
                final_sect = child
                continue
            if final_sect is not None and (
                child.tag in _BLOCK_TAGS or child.tag == qn("w:sdt")
            ):
                trailing += 1
                continue
            index = cls._scan(child, index, sections, owners)

        sections.append(
            Section.parse(final_sect, index, None)
            if final_sect is not None
            # A body with no final w:sectPr is invalid but occurs in the wild
            # (some HTML-to-docx converters). Synthesising an empty section
            # keeps every downstream index valid; `problems` reports it.
            else Section.parse(etree.Element(qn("w:sectPr")), index, None)
        )
        model = cls(sections, settings, trailing_blocks=trailing)
        model._by_element = owners
        return model

    @classmethod
    def _scan(
        cls,
        el: etree._Element,
        index: int,
        sections: list[Section],
        owners: dict[etree._Element, int],
    ) -> int:
        """Assign `el` to the current section, advancing past section breaks."""
        tag = el.tag
        if tag == qn("w:p"):
            owners[el] = index
            sect = el.find(f"{qn('w:pPr')}/{qn('w:sectPr')}")
            if sect is not None:
                # The paragraph carrying the break is the LAST paragraph of
                # the section it describes, not the first of the next one.
                sections.append(Section.parse(sect, index, el))
                return index + 1
            return index
        if tag in _BLOCK_TAGS:
            owners[el] = index
            return index
        if tag == qn("w:sdt"):
            content = el.find(qn("w:sdtContent"))
            if content is None:
                return index
            el = content
        elif tag not in _TRANSPARENT:
            return index
        for child in el:
            if isinstance(child.tag, str):
                index = cls._scan(child, index, sections, owners)
        return index

    # -- lookup ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self.sections)

    def __iter__(self):
        return iter(self.sections)

    def __getitem__(self, index: int) -> Section:
        return self.sections[index]

    def section_of(self, el: etree._Element) -> int:
        """Which section an element belongs to, walking up to a body child.

        A paragraph inside a table cell answers with its table's section,
        which is the only sensible answer: a table cannot straddle a break.
        """
        node = el
        while node is not None:
            if node in self._by_element:
                return self._by_element[node]
            node = node.getparent()
        return len(self.sections) - 1

    # -- header / footer resolution -------------------------------------

    def active_types(self, index: int) -> tuple[str, ...]:
        """Which of the three slots Word will actually render for a section.

        "first" needs w:titlePg on THIS section; "even" needs
        w:evenAndOddHeaders in settings.xml, which is document-wide. A slot
        that is not active is not rendered no matter what it references.
        """
        types = ["default"]
        if self.settings.even_and_odd_headers:
            types.insert(0, "even")
        if 0 <= index < len(self.sections) and self.sections[index].title_page:
            types.insert(0, "first")
        return tuple(types)

    def resolve(self, index: int, kind: str, type: str) -> HdrFtrRef:
        """One slot, with link-to-previous inheritance applied.

        Inheritance is per (kind, type): unlinking the first-page header does
        not unlink the default one, so each of the six slots walks back
        independently until it finds a section that declared it.
        """
        attr = "header_rids" if kind == "header" else "footer_rids"
        active = type in self.active_types(index)
        for i in range(min(index, len(self.sections) - 1), -1, -1):
            rid = getattr(self.sections[i], attr).get(type)
            if rid:
                return HdrFtrRef(
                    kind=kind,
                    type=type,
                    part=self._part_for(rid),
                    defined_in=i,
                    inherited=i != index,
                    active=active,
                )
        # Nothing to inherit. Word renders an empty header here, which is how
        # a cover page legitimately gets no header at all.
        return HdrFtrRef(kind, type, None, None, False, active)

    def _part_for(self, rid: str) -> str | None:
        if self.pkg is None or self.part is None:
            return None
        rel = self.pkg.rels(self.part).get(rid)
        if rel is None or rel.external:
            return None
        target = rel.resolve(self.part)
        return self.pkg.actual_name(target) if target else None

    def slots(self, index: int, active_only: bool = True) -> list[HdrFtrRef]:
        """Every header/footer slot of one section."""
        out = []
        for kind in ("header", "footer"):
            for type in HDRFTR_TYPES:
                ref = self.resolve(index, kind, type)
                if ref.active or not active_only:
                    out.append(ref)
        return out

    def used_parts(self) -> set[str]:
        """Header/footer parts that some active slot actually renders."""
        return {
            ref.part
            for i in range(len(self.sections))
            for ref in self.slots(i)
            if ref.part
        }

    def orphan_parts(self) -> list[str]:
        """Header/footer parts present in the package but never rendered.

        Usually even-page headers left behind after someone turned off
        "Different Odd & Even Pages": real, resolvable, and invisible. They
        are scrub candidates for the donor and a lint note elsewhere.
        """
        if self.pkg is None:
            return []
        present: list[str] = []
        for reltype in ("header", "footer"):
            present.extend(dict.fromkeys(self.pkg.related_all(RT[reltype])))
        used = self.used_parts()
        return [p for p in present if p not in used]

    # -- diagnostics ----------------------------------------------------

    def linked_to_previous(self, index: int) -> list[HdrFtrRef]:
        """Slots this section inherits rather than declares.

        The reason to surface this: inheriting is the DEFAULT, so a document
        that meant to show no header in a later section silently shows the
        previous one instead. The remedy is an explicitly empty part.
        """
        return [ref for ref in self.slots(index) if ref.inherited]

    def problems(self) -> list[str]:
        out: list[str] = []
        if self.trailing_blocks:
            out.append(
                f"{self.trailing_blocks} block(s) follow the body-level w:sectPr; "
                "Word treats this as corruption and offers to repair the file."
            )
        for sect in self.sections:
            if sect.page.width is None or sect.page.height is None:
                out.append(f"section {sect.index} has no w:pgSz; page size is undefined.")
            width = sect.content_width
            if width is not None and width.twips <= 0:
                out.append(
                    f"section {sect.index} margins leave no text width "
                    f"({width}); content will not fit the page."
                )
            if sect.columns.count > 1 and not sect.columns.equal_width:
                if len(sect.columns.widths) < sect.columns.count:
                    out.append(
                        f"section {sect.index} declares {sect.columns.count} unequal "
                        f"columns but only {len(sect.columns.widths)} w:col widths."
                    )
        if self.settings.even_and_odd_headers:
            missing = [
                s.index for s in self.sections
                if not self.resolve(s.index, "header", "even").part
                and self.resolve(s.index, "header", "default").part
            ]
            if missing:
                out.append(
                    "w:evenAndOddHeaders is on but sections "
                    f"{missing} have no even-page header; those pages render blank."
                )
        return out
