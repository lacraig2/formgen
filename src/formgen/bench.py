"""Discover a template's fillable slots and fill them -- the engine behind
both the local server (`tools/serve.py`) and the single-file browser build
(`tools/build_single_html.py`), with no HTTP or transport of its own.

Everything here is pure `bytes in, bytes out` over the real `find_fields` /
`find_repeats` / `fill`, so the same three calls run on a desktop and inside
Pyodide unchanged. The browser build owes its existence to that: there is no
second implementation, only this thin adapter over the engine.
"""

from __future__ import annotations

import tempfile
from collections import OrderedDict
from pathlib import Path

from .content.fill import fill
from .content.presentation import is_presentation
from .content.session import (
    encode_answers, read_session, template_of, write_session,
)
from .learn.formfields import (
    CHECKBOX, CHOICE, DATE, IMAGE, NUMBER, find_fields, find_repeats,
)
from .opc.package import OpcPackage

_KINDS = {IMAGE: "image", CHECKBOX: "checkbox", CHOICE: "choice",
          DATE: "date", NUMBER: "number"}


def _open(data: bytes) -> OpcPackage:
    # A directory, not NamedTemporaryFile: Windows will not let the package be
    # reopened while the temp file's own handle is still open. `open` reads the
    # bytes into memory, so the file is gone by the time the caller uses it.
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "in.docx"
        path.write_bytes(data)
        return OpcPackage.open(path)


def _save(pkg: OpcPackage) -> bytes:
    with tempfile.TemporaryDirectory() as out:
        target = Path(out) / "out.docx"
        pkg.save(target, deterministic=True)
        return target.read_bytes()


def prettify(name: str) -> str:
    return name.replace("_", " ").strip().title() or name


def inspect(data: bytes) -> dict:
    """A template's fillable slots, de-duplicated, ready to build a form from.

    Returns ``{"fields": [...], "groups": [...]}`` -- single fields (each with
    name, kind, label, current value, and any choices) and repeating groups
    (each with its columns). If the document is a *self-refilling* one -- a
    finished document carrying its own blank template and last answers -- the
    form is read from that embedded template and a ``"prefill"`` of the saved
    answers rides along, so reopening a filled document reconstructs its form.
    """
    session = read_session(_open(data))
    if session is not None:
        result = _inspect(template_of(session))
        result["prefill"] = session.get("answers", {})
        return result
    return _inspect(data)


def _inspect(data: bytes) -> dict:
    pkg = _open(data)
    flavor = "pptx" if is_presentation(pkg) else "docx"
    fields: "OrderedDict[str, dict]" = OrderedDict()
    for item in find_fields(pkg).fields:
        if not item.name or item.name in fields:
            continue
        kind = _KINDS.get(item.kind, "text")
        fields[item.name] = {
            "name": item.name,
            "kind": kind,
            "label": item.label or prettify(item.name),
            "value": "" if kind == "image" else item.value,
            "choices": list(item.choices),
            "required": item.required,
        }
    groups = [
        {"name": group.name, "label": prettify(group.name),
         "columns": [{"name": col.name, "label": col.label,
                      "kind": _KINDS.get(col.kind, "text"),
                      "choices": list(col.choices)}
                     for col in group.columns]}
        for group in find_repeats(pkg)
    ]
    return {"fields": list(fields.values()), "groups": groups, "kind": flavor}


def fill_document(data: bytes, text: dict | None = None,
                  checks: dict | None = None, images: dict | None = None,
                  groups: dict | None = None) -> tuple[bytes, dict]:
    """Fill a template and hand back the finished ``.docx`` bytes and a report.

    `text` maps name->str, `checks` name->bool, `images` name->bytes, `groups`
    collection->list-of-record-dicts. The report mirrors `fill`'s: what was
    filled, cleared, left alone, and which supplied keys matched nothing.

    The finished document is *self-refilling*: it carries the blank template it
    was filled from and the answers used, so it can be reopened and edited. If
    `data` was itself such a document, its embedded template -- not its filled
    body -- is what gets filled, so re-editing starts from a clean form.
    """
    session = read_session(_open(data))
    template = template_of(session) if session is not None else data

    pkg = _open(template)
    values: dict = dict(text or {})
    for name, on in (checks or {}).items():
        values[name] = bool(on)
    for name, blob in (images or {}).items():
        if isinstance(blob, (bytes, bytearray)):
            values[name] = bytes(blob)
    for name, records in (groups or {}).items():
        if isinstance(records, list):
            values[name] = records
    report = fill(pkg, values)
    kind = "pptx" if is_presentation(pkg) else "docx"
    write_session(pkg, template, encode_answers(text, checks, images, groups))
    info = {
        "kind": kind,
        "summary": report.summary(),
        "filled": sorted(set(report.filled)),
        "cleared": sorted(set(report.cleared)),
        "untouched": sorted(set(report.untouched)),
        "unknown": sorted(report.unknown),
        "leftover": list(report.leftover),
    }
    return _save(pkg), info


def learn(docs: list[bytes]) -> dict:
    """Derive a marked-up template from filled copies of one form.

    `docs` is two or more filled ``.docx`` byte strings. Returns the template as
    base64 (ready to hand straight back to `inspect`/`fill_document`) plus the
    fields found and any warnings -- the authoring shortcut that needs no
    marker typing at all.
    """
    import base64 as _b64

    from .learn.byexample import learn_template
    template, info = learn_template(list(docs))
    return {"template": _b64.b64encode(template).decode("ascii"), **info}


def starter() -> bytes:
    """A tiny, theme-free, schema-valid ``.docx`` demonstrating every marker.

    Theme-free on purpose: an incomplete theme is what makes Word offer to
    "recover" a document, so this carries none and names its fonts outright.
    """
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

    def para(text: str, style: str = "") -> str:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        run = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
        return f"<w:p>{ppr}{run}</w:p>"

    def style(sid: str, name: str, sz: int, *, bold: bool = False,
              center: bool = False) -> str:
        b = "<w:b/>" if bold else ""
        jc = '<w:jc w:val="center"/>' if center else ""
        based = "" if sid == "Normal" else '<w:basedOn w:val="Normal"/>'
        return (f'<w:style w:type="paragraph" w:styleId="{sid}">'
                f'<w:name w:val="{name}"/>{based}<w:pPr>{jc}</w:pPr>'
                f'<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
                f'<w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/>{b}</w:rPr></w:style>')

    def cell(text: str) -> str:
        return ('<w:tc><w:tcPr><w:tcW w:w="4680" w:type="dxa"/></w:tcPr>'
                f"{para(text, 'BodyText')}</w:tc>")

    borders = "".join(
        f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
        for edge in ("top", "left", "bottom", "right", "insideH", "insideV"))
    table = (
        '<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/>'
        f"<w:tblBorders>{borders}</w:tblBorders></w:tblPr>"
        '<w:tblGrid><w:gridCol w:w="4680"/><w:gridCol w:w="4680"/></w:tblGrid>'
        + "<w:tr>" + cell("Material") + cell("Quantity") + "</w:tr>"
        + "<w:tr>" + cell("{{items.material}}") + cell("{{items.quantity}}") + "</w:tr>"
        + "</w:tbl>"
    )
    body = (
        para("Lab Report", "Title")
        + para("Prepared by {{full_name}} on {{date}}.", "BodyText")
        + para("Report number: {{report_no}}", "BodyText")
        + para("Cleared for release: {{check: cleared}}    "
               "Status: {{choice: status | Draft, Final}}", "BodyText")
        + para("Summary", "Heading1")
        + para("Type your text here. A marker like {{margin}} becomes an "
               "input on the form. A multi-line value keeps its lines.",
               "BodyText")
        + para("Materials", "Heading1")
        + para("Each row you add on the form is one line in this table:",
               "BodyText")
        + table
        + para("Figure", "Heading1")
        + para("{{image: figure}}", "BodyText")
        + para("Figure 1. {{caption}}", "Caption")
    )
    sect = ('<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1440" '
            'w:right="1440" w:bottom="1440" w:left="1440" w:header="720" '
            'w:footer="720" w:gutter="0"/></w:sectPr>')
    decl = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    blobs = {
        "[Content_Types].xml": (decl +
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Default Extension="png" ContentType="image/png"/>'
            '<Default Extension="jpeg" ContentType="image/jpeg"/>'
            '<Default Extension="jpg" ContentType="image/jpeg"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>").encode(),
        "_rels/.rels": (decl +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            "</Relationships>").encode(),
        "word/document.xml": (decl +
            f"<w:document {w} {r}><w:body>{body}{sect}</w:body></w:document>").encode(),
        "word/_rels/document.xml.rels": (decl +
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
            "</Relationships>").encode(),
        "word/styles.xml": (decl + f"<w:styles {w}>"
            '<w:docDefaults><w:rPrDefault><w:rPr>'
            '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/>'
            "</w:rPr></w:rPrDefault></w:docDefaults>"
            + style("Normal", "Normal", 22)
            + style("Title", "Title", 56, bold=True, center=True)
            + style("Heading1", "heading 1", 32, bold=True)
            + style("BodyText", "Body Text", 22)
            + style("Caption", "caption", 18)
            + "</w:styles>").encode(),
        "word/settings.xml": (decl +
            f'<w:settings {w}><w:defaultTabStop w:val="720"/></w:settings>').encode(),
        "docProps/core.xml": (decl +
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>formgen starter template</dc:title>"
            "<dc:creator>formgen</dc:creator></cp:coreProperties>").encode(),
    }
    return _save(OpcPackage(blobs))
