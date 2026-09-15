"""XML namespaces and qualified-name helpers.

One module owns every namespace URI in the project so that prefixes stay
consistent across parsing, serialization and XPath.
"""

from __future__ import annotations

NS = {
    # WordprocessingML
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    # Relationships (the r:id attribute namespace, distinct from the rels part)
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    # DrawingML
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "asvg": "http://schemas.microsoft.com/office/drawing/2016/SVG/main",
    # Office Math
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "w16se": "http://schemas.microsoft.com/office/word/2015/wordml/symex",
    "wp14": "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    # Markup compatibility (mc:AlternateContent)
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "wps": "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    # VML (legacy shapes, textboxes, OLE previews)
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    # Package-level
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    # Document properties
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "op": "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
    "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

# Relationship type URIs. Part names like "word/styles.xml" are conventional,
# not normative -- always resolve through these instead of hardcoding paths.
_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"

RT = {
    "officeDocument": f"{_OFFICE_REL}/officeDocument",
    "styles": f"{_OFFICE_REL}/styles",
    "numbering": f"{_OFFICE_REL}/numbering",
    "settings": f"{_OFFICE_REL}/settings",
    "webSettings": f"{_OFFICE_REL}/webSettings",
    "fontTable": f"{_OFFICE_REL}/fontTable",
    "theme": f"{_OFFICE_REL}/theme",
    "footnotes": f"{_OFFICE_REL}/footnotes",
    "endnotes": f"{_OFFICE_REL}/endnotes",
    "comments": f"{_OFFICE_REL}/comments",
    "header": f"{_OFFICE_REL}/header",
    "footer": f"{_OFFICE_REL}/footer",
    "image": f"{_OFFICE_REL}/image",
    "hyperlink": f"{_OFFICE_REL}/hyperlink",
    "oleObject": f"{_OFFICE_REL}/oleObject",
    "chart": f"{_OFFICE_REL}/chart",
    "attachedTemplate": f"{_OFFICE_REL}/attachedTemplate",
    # Imported content Word renders inline but that lives in its own part.
    "aFChunk": f"{_OFFICE_REL}/aFChunk",
    "glossaryDocument": f"{_OFFICE_REL}/glossaryDocument",
    "extended": f"{_OFFICE_REL}/extended-properties",
    "custom": f"{_OFFICE_REL}/custom-properties",
    "core": f"{_PKG_REL}/metadata/core-properties",
    # Word 2010+ comment sidecars
    "commentsExtended": (
        "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
    ),
    "commentsIds": "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
    "people": "http://schemas.microsoft.com/office/2011/relationships/people",
}


def qn(tag: str) -> str:
    """'w:p' -> '{http://...wordprocessingml/2006/main}p'."""
    prefix, _, local = tag.partition(":")
    if not local:
        return tag
    try:
        return f"{{{NS[prefix]}}}{local}"
    except KeyError as exc:  # pragma: no cover - programmer error
        raise KeyError(f"unknown namespace prefix {prefix!r} in {tag!r}") from exc


def nsplit(clark: str) -> tuple[str | None, str]:
    """'{uri}local' -> (uri, 'local'). Unqualified names yield (None, name)."""
    if clark.startswith("{"):
        uri, _, local = clark[1:].partition("}")
        return uri, local
    return None, clark


def local_name(clark: str) -> str:
    return nsplit(clark)[1]
