"""Build .docx fixtures from XML templates with pinned ids.

Fixtures are code, not checked-in binaries, because docx output is otherwise
unreproducible (rsids, paraIds, docPr ids, timestamps, rId ordering). Building
them through our own OPC writer has the useful side effect of exercising the
writer on every test run.
"""

from __future__ import annotations

from formgen.opc.content_types import CT
from formgen.opc.package import OpcPackage

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

_CONTENT_TYPES = f"""{DECL}
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="{CT['rels']}"/>
<Default Extension="xml" ContentType="{CT['xml']}"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="{CT['document']}"/>
<Override PartName="/word/styles.xml" ContentType="{CT['styles']}"/>
<Override PartName="/word/numbering.xml" ContentType="{CT['numbering']}"/>
<Override PartName="/word/settings.xml" ContentType="{CT['settings']}"/>
<Override PartName="/word/theme/theme1.xml" ContentType="{CT['theme']}"/>
<Override PartName="/docProps/core.xml" ContentType="{CT['core']}"/>
</Types>"""

_ROOT_RELS = f"""{DECL}
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>"""

_DOC_RELS = f"""{DECL}
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>
</Relationships>"""

_CORE = f"""{DECL}
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>{{title}}</dc:title>
<dc:creator>{{creator}}</dc:creator>
<cp:revision>1</cp:revision>
<dcterms:created xsi:type="dcterms:W3CDTF">2024-01-01T00:00:00Z</dcterms:created>
</cp:coreProperties>"""

_THEME = f"""{DECL}
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office">
<a:themeElements>
<a:clrScheme name="Office">
<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
<a:dk2><a:srgbClr val="44546A"/></a:dk2>
<a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>
<a:accent1><a:srgbClr val="4472C4"/></a:accent1>
<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>
<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>
<a:accent4><a:srgbClr val="FFC000"/></a:accent4>
<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>
<a:accent6><a:srgbClr val="70AD47"/></a:accent6>
<a:hlink><a:srgbClr val="0563C1"/></a:hlink>
<a:folHlink><a:srgbClr val="954F72"/></a:folHlink>
</a:clrScheme>
<a:fontScheme name="Office">
<a:majorFont><a:latin typeface="{{major}}"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>
<a:minorFont><a:latin typeface="{{minor}}"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>
</a:fontScheme>
<a:fmtScheme name="Office"/>
</a:themeElements>
</a:theme>"""

_SETTINGS = f"""{DECL}
<w:settings {W}>
<w:defaultTabStop w:val="720"/>{{extra}}
<w:compat><w:compatSetting w:name="compatibilityMode" w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat>
</w:settings>"""

_NUMBERING = f"""{DECL}
<w:numbering {W}>
<w:abstractNum w:abstractNumId="0">
<w:nsid w:val="0A1B2C3D"/>
<w:multiLevelType w:val="hybridMultilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr><w:rPr><w:rFonts w:ascii="Symbol" w:hAnsi="Symbol"/></w:rPr></w:lvl>
</w:abstractNum>
<w:abstractNum w:abstractNumId="1">
<w:nsid w:val="1B2C3D4E"/>
<w:multiLevelType w:val="multilevel"/>
<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="360" w:hanging="360"/></w:pPr></w:lvl>
<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2)"/><w:lvlJc w:val="left"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>
</w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
</w:numbering>"""

# docDefaults deliberately carries the body font/size, with Normal empty, so
# tests exercise the "docDefaults is the inheritance root, not Normal" trap.
_STYLES_HEAD = f"""{DECL}
<w:styles {W}>
<w:docDefaults>
<w:rPrDefault><w:rPr><w:rFonts w:asciiTheme="minorHAnsi" w:hAnsiTheme="minorHAnsi"/><w:sz w:val="{{default_sz}}"/><w:szCs w:val="{{default_sz}}"/><w:lang w:val="en-US"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
<w:latentStyles w:defLockedState="0" w:defUIPriority="99" w:defSemiHidden="0" w:defUnhideWhenUsed="0" w:defQFormat="0" w:count="371">
<w:lsdException w:name="Normal" w:uiPriority="0" w:qFormat="1"/>
<w:lsdException w:name="heading 1" w:uiPriority="9" w:qFormat="1"/>
</w:latentStyles>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>"""

# w:outlineLvl is a child of w:pPr, not a sibling of it -- getting this
# wrong silently costs you the strongest heading signal in the document.
_STYLE_TMPL = """<w:style w:type="{type}" w:styleId="{sid}">
<w:name w:val="{name}"/>{based}{nxt}{link}
<w:pPr>{outline}{ppr}</w:pPr><w:rPr>{rpr}</w:rPr></w:style>"""


def style(
    sid: str,
    name: str,
    *,
    type: str = "paragraph",
    based_on: str | None = "Normal",
    next_style: str | None = None,
    link: str | None = None,
    outline: int | None = None,
    ppr: str = "",
    rpr: str = "",
) -> str:
    return _STYLE_TMPL.format(
        type=type,
        sid=sid,
        name=name,
        based=f'<w:basedOn w:val="{based_on}"/>' if based_on else "",
        nxt=f'<w:next w:val="{next_style}"/>' if next_style else "",
        link=f'<w:link w:val="{link}"/>' if link else "",
        outline=f'<w:outlineLvl w:val="{outline}"/>' if outline is not None else "",
        ppr=ppr,
        rpr=rpr,
    )


DEFAULT_STYLES = [
    style("Title", "Title", rpr='<w:sz w:val="56"/><w:b/>', ppr='<w:jc w:val="center"/>'),
    style("Heading1", "heading 1", outline=0, next_style="BodyText",
          rpr='<w:sz w:val="32"/><w:b/>', ppr='<w:keepNext/><w:spacing w:before="240"/>'),
    style("Heading2", "heading 2", outline=1, next_style="BodyText",
          rpr='<w:sz w:val="26"/><w:b/>', ppr='<w:keepNext/>'),
    style("BodyText", "Body Text", next_style="BodyText",
          ppr='<w:spacing w:after="120"/><w:jc w:val="both"/>'),
    style("Caption", "caption", rpr='<w:sz w:val="18"/><w:i/>'),
    style("ListParagraph", "List Paragraph", ppr='<w:ind w:left="720"/>'),
    style("Hyperlink", "Hyperlink", type="character", based_on=None,
          rpr='<w:color w:val="0563C1"/><w:u w:val="single"/>'),
]

SECT_PR = (
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
    '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
    'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
)


def para(text: str = "", *, style: str | None = None, ppr_extra: str = "",
         rpr: str = "", numid: int | None = None, ilvl: int = 0,
         runs: str = "") -> str:
    """One <w:p>. `rpr` applies direct run formatting to the whole paragraph.

    `runs` is raw run-level XML appended after the text run -- form fields,
    content controls, anything a fixture needs to hold inside a paragraph.
    """
    bits = []
    if style:
        bits.append(f'<w:pStyle w:val="{style}"/>')
    if numid is not None:
        bits.append(f'<w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{numid}"/></w:numPr>')
    bits.append(ppr_extra)
    ppr = f"<w:pPr>{''.join(bits)}</w:pPr>" if any(bits) else ""
    run = f'<w:r>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f"<w:p>{ppr}{run}{runs}</w:p>"


def document(body: str, sect_pr: str | None = None) -> str:
    """One w:document. `sect_pr` overrides the final, body-level w:sectPr."""
    tail = SECT_PR if sect_pr is None else sect_pr
    return f'{DECL}\n<w:document {W} {R}><w:body>{body}{tail}</w:body></w:document>'


def styles_xml(extra: list[str] | None = None, default_sz: int = 22) -> str:
    parts = [_STYLES_HEAD.format(default_sz=default_sz)]
    parts.extend(DEFAULT_STYLES if extra is None else extra)
    parts.append("</w:styles>")
    return "".join(parts)


def make(
    body: str | None = None,
    *,
    styles: list[str] | None = None,
    default_sz: int = 22,
    sect_pr: str | None = None,
    settings_extra: str = "",
    numbering: str | None = None,
    title: str = "Fixture",
    creator: str = "formgen tests",
    major: str = "Calibri Light",
    minor: str = "Calibri",
    extra_parts: dict[str, bytes] | None = None,
) -> OpcPackage:
    """Assemble a complete, valid package."""
    if body is None:
        body = (
            para("A Fixture Document", style="Title")
            + para("Introduction", style="Heading1")
            + para("The body text of the document.", style="BodyText")
        )
    blobs = {
        "[Content_Types].xml": _CONTENT_TYPES.encode(),
        "_rels/.rels": _ROOT_RELS.encode(),
        "word/document.xml": document(body, sect_pr).encode(),
        "word/_rels/document.xml.rels": _DOC_RELS.encode(),
        "word/styles.xml": styles_xml(styles, default_sz).encode(),
        "word/numbering.xml": (numbering or _NUMBERING).encode(),
        "word/settings.xml": _SETTINGS.format(extra=settings_extra).encode(),
        "word/theme/theme1.xml": _THEME.format(major=major, minor=minor).encode(),
        "docProps/core.xml": _CORE.format(title=title, creator=creator).encode(),
    }
    if extra_parts:
        blobs.update(extra_parts)
    return OpcPackage(blobs)


# -- sections -------------------------------------------------------------


def sectpr(
    *,
    w: int = 12240,
    h: int = 15840,
    orient: str | None = None,
    margins: str = 'w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
                   'w:header="720" w:footer="720" w:gutter="0"',
    headers: dict[str, str] | None = None,
    footers: dict[str, str] | None = None,
    title_page: bool = False,
    break_type: str | None = None,
    cols: str = "",
    extra: str = "",
) -> str:
    """A w:sectPr. `headers`/`footers` map type -> rId."""
    refs = "".join(
        f'<w:headerReference w:type="{t}" r:id="{rid}"/>'
        for t, rid in (headers or {}).items()
    ) + "".join(
        f'<w:footerReference w:type="{t}" r:id="{rid}"/>'
        for t, rid in (footers or {}).items()
    )
    orient_attr = f' w:orient="{orient}"' if orient else ""
    return (
        f"<w:sectPr>{refs}"
        f'{f"<w:type w:val={break_type!r}/>" if break_type else ""}'
        f'<w:pgSz w:w="{w}" w:h="{h}"{orient_attr}/>'
        f"<w:pgMar {margins}/>"
        f"{cols}"
        f'{"<w:titlePg/>" if title_page else ""}'
        f"{extra}</w:sectPr>"
    )


def break_para(sect_pr: str, text: str = "") -> str:
    """The last paragraph of a section: it CARRIES that section's w:sectPr."""
    run = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f"<w:p><w:pPr>{sect_pr}</w:pPr>{run}</w:p>"


HDRFTR_CT = {
    "header": "application/vnd.openxmlformats-officedocument."
              "wordprocessingml.header+xml",
    "footer": "application/vnd.openxmlformats-officedocument."
              "wordprocessingml.footer+xml",
}


def add_hdrftr(pkg, kind: str, name: str, text: str = "") -> str:
    """Add a header/footer part to a package and relate it. Returns the rId."""
    from formgen.opc.ns import RT

    xml = (
        f'{DECL}\n<w:{kind} {W}>{para(text) if text else "<w:p/>"}</w:{kind}>'
    )
    pkg.add_part(f"word/{name}", xml.encode(), HDRFTR_CT[kind])
    return pkg.relate(RT[kind], f"word/{name}", pkg.main_document)


def form_text(name: str = "Text1", result: str = "", default: str | None = None) -> str:
    """A legacy FORMTEXT field: begin+ffData, instruction, separate, result, end."""
    dflt = f'<w:default w:val="{default}"/>' if default is not None else ""
    return (
        "<w:r><w:fldChar w:fldCharType=\"begin\">"
        f'<w:ffData><w:name w:val="{name}"/><w:enabled/>'
        f"<w:textInput>{dflt}</w:textInput></w:ffData>"
        "</w:fldChar></w:r>"
        "<w:r><w:instrText xml:space=\"preserve\"> FORMTEXT </w:instrText></w:r>"
        "<w:r><w:fldChar w:fldCharType=\"separate\"/></w:r>"
        f'<w:r><w:t xml:space="preserve">{result or chr(0x2002) * 5}</w:t></w:r>'
        "<w:r><w:fldChar w:fldCharType=\"end\"/></w:r>"
    )


def form_checkbox(name: str = "Check1", checked: bool = False) -> str:
    state = '<w:checked/>' if checked else '<w:checked w:val="0"/>'
    return (
        "<w:r><w:fldChar w:fldCharType=\"begin\">"
        f'<w:ffData><w:name w:val="{name}"/><w:enabled/>'
        f"<w:checkBox><w:sizeAuto/>{state}</w:checkBox></w:ffData>"
        "</w:fldChar></w:r>"
        "<w:r><w:instrText xml:space=\"preserve\"> FORMCHECKBOX </w:instrText></w:r>"
        "<w:r><w:fldChar w:fldCharType=\"separate\"/></w:r>"
        "<w:r><w:t> </w:t></w:r>"
        "<w:r><w:fldChar w:fldCharType=\"end\"/></w:r>"
    )


def cell(*paragraphs: str) -> str:
    return "<w:tc><w:tcPr/>" + "".join(paragraphs) + "</w:tc>"


def table(*rows: str) -> str:
    return ("<w:tbl><w:tblPr/><w:tblGrid/>"
            + "".join(f"<w:tr>{r}</w:tr>" for r in rows) + "</w:tbl>")
