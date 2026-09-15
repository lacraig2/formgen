"""[Content_Types].xml -- the map from part name to media type.

Every part in an OPC package must have a content type, resolved either by its
file extension (a Default entry) or by an exact part-name match (an Override).
A part with no resolvable content type makes Word refuse to open the file, so
:meth:`ContentTypes.validate` is part of the pre-write invariant suite.
"""

from __future__ import annotations

import posixpath

from lxml import etree

from .ns import NS


def fold(name: str) -> str:
    """Case-fold a part name for OPC equivalence.

    OPC part names compare case-insensitively, so two Overrides differing only
    in case are the same part name and make the package non-conforming. ASCII
    lowering is deliberate: str.lower() is Unicode-aware and turns a dotted
    capital I into two characters, which is not how the filesystem or the
    spec compares names.
    """
    return name.lstrip("/").translate(_ASCII_LOWER)


_ASCII_LOWER = {c: c + 32 for c in range(ord("A"), ord("Z") + 1)}

PART_NAME = "[Content_Types].xml"

# Content types we mint when adding parts.
CT = {
    "rels": "application/vnd.openxmlformats-package.relationships+xml",
    "xml": "application/xml",
    "document": (
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document.main+xml"
    ),
    "styles": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
    ),
    "numbering": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
    ),
    "settings": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
    ),
    "header": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
    ),
    "footer": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
    ),
    "footnotes": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
    ),
    "endnotes": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"
    ),
    "comments": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
    ),
    "theme": "application/vnd.openxmlformats-officedocument.theme+xml",
    "fontTable": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"
    ),
    "core": "application/vnd.openxmlformats-package.core-properties+xml",
    "extended": (
        "application/vnd.openxmlformats-officedocument.extended-properties+xml"
    ),
    "custom": "application/vnd.openxmlformats-officedocument.custom-properties+xml",
}

# Extensions we may need to add Default entries for when copying media in.
MEDIA_CT = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "emf": "image/x-emf",
    "wmf": "image/x-wmf",
    "svg": "image/svg+xml",
    "bin": "application/vnd.openxmlformats-officedocument.oleObject",
}


class ContentTypes:
    """Parsed [Content_Types].xml, preserving entry order."""

    def __init__(self, defaults: dict[str, str], overrides: dict[str, str]):
        # extension (lowercased) -> content type
        self.defaults = defaults
        # folded part name -> (original-cased part name, content type)
        self._overrides: dict[str, tuple[str, str]] = {}
        for name, ct in overrides.items():
            self._overrides[fold(name)] = (name.lstrip("/"), ct)

    @property
    def overrides(self) -> dict[str, str]:
        """Overrides keyed by their original-cased part name."""
        return {name: ct for name, ct in self._overrides.values()}

    @classmethod
    def parse(cls, blob: bytes) -> ContentTypes:
        root = etree.fromstring(blob)
        defaults: dict[str, str] = {}
        overrides: dict[str, str] = {}
        for el in root:
            # Comments and processing instructions have a callable .tag;
            # etree.QName() raises on them.
            if not isinstance(el.tag, str):
                continue
            tag = etree.QName(el).localname
            if tag == "Default":
                # An entry missing its key is garbage Word ignores. Keeping it
                # would round-trip into Extension="" -- garbage Word rejects.
                if ext := (el.get("Extension") or "").strip():
                    defaults[ext.lower()] = el.get("ContentType", "")
            elif tag == "Override":
                if name := (el.get("PartName") or "").strip().lstrip("/"):
                    overrides[name] = el.get("ContentType", "")
        return cls(defaults, overrides)

    def serialize(self) -> bytes:
        root = etree.Element(f"{{{NS['ct']}}}Types", nsmap={None: NS["ct"]})
        for ext, ct in self.defaults.items():
            etree.SubElement(
                root, f"{{{NS['ct']}}}Default", Extension=ext, ContentType=ct
            )
        for name, ct in self._overrides.values():
            etree.SubElement(
                root,
                f"{{{NS['ct']}}}Override",
                PartName="/" + name,
                ContentType=ct,
            )
        return etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    def for_part(self, partname: str) -> str | None:
        """Resolve a part's content type: Override wins over Default."""
        if hit := self._overrides.get(fold(partname)):
            return hit[1]
        # The extension comes from the basename; a dot in a directory name
        # must not be mistaken for the part's extension.
        _, _, ext = posixpath.basename(partname).rpartition(".")
        return self.defaults.get(ext.lower()) if ext else None

    def ensure_default(self, ext: str, content_type: str) -> None:
        self.defaults.setdefault(ext.lower(), content_type)

    def ensure_media_default(self, partname: str) -> None:
        """Add a Default entry for a media part's extension if it has none."""
        _, _, ext = posixpath.basename(partname).rpartition(".")
        ext = ext.lower()
        if ext and ext not in self.defaults and ext in MEDIA_CT:
            self.defaults[ext] = MEDIA_CT[ext]

    def set_override(self, partname: str, content_type: str) -> None:
        self._overrides[fold(partname)] = (partname.lstrip("/"), content_type)

    def drop(self, partname: str) -> None:
        self._overrides.pop(fold(partname), None)

    def has_override(self, partname: str) -> bool:
        return fold(partname) in self._overrides

    def validate(self, partnames) -> list[str]:
        """Return the names of parts with no resolvable content type."""
        missing = []
        for name in partnames:
            if name == PART_NAME:
                continue
            if self.for_part(name) is None:
                missing.append(name)
        return missing
