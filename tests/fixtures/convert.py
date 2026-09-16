"""Turn a styled package into the state half of all real documents are in.

Google Docs export, PDF-to-Word conversion and plain hand-formatting all
produce the same thing: every paragraph is `Normal`, the styles that used to
carry the format are gone from `styles.xml`, and everything the reader sees
is written out as direct formatting on the runs. 48% of the Apache POI
corpus is in exactly this state.

Simulating it in a fixture is the only way to test the recovery path
honestly. The output *looks* identical to its input in Word -- that is the
whole point, and it is why the format is still recoverable at all.
"""

from __future__ import annotations

from lxml import etree

from formgen.opc.ns import RT, qn
from formgen.opc.package import OpcPackage
from formgen.oox.props import ParaProps, RunProps
from formgen.oox.styles import StyleGraph
from formgen.oox.theme import Theme
from formgen.oox.walk import Walker

_KEPT_STYLES = {"normal", "default paragraph font", "normal table", "no list"}
_DROPPED_PPR = ("w:pStyle", "w:spacing", "w:ind", "w:jc", "w:outlineLvl",
                "w:keepNext")


def _part(pkg: OpcPackage, reltype: str):
    name = pkg.related(RT[reltype])
    return pkg.element(name) if name and name in pkg else None


def flatten(pkg: OpcPackage) -> OpcPackage:
    """Resolve every style into direct formatting, then delete the styles."""
    theme = Theme.parse(_part(pkg, "theme"))
    styles = StyleGraph.parse(_part(pkg, "styles"), theme)

    for block in Walker(pkg).blocks(include_aux=False):
        if not block.is_paragraph:
            continue
        paragraph = block.element
        style_id = styles.para_style_or_default(block.style_id)
        ppr = paragraph.find(qn("w:pPr"))
        para = styles.effective_for_para(style_id, ParaProps.parse(ppr))
        if ppr is None:
            ppr = etree.Element(qn("w:pPr"))
            paragraph.insert(0, ppr)
        for child in list(ppr):
            if child.tag in {qn(t) for t in _DROPPED_PPR}:
                ppr.remove(child)
        if para.alignment:
            etree.SubElement(ppr, qn("w:jc")).set(qn("w:val"), para.alignment)
        spacing = etree.SubElement(ppr, qn("w:spacing"))
        if para.space_before is not None:
            spacing.set(qn("w:before"), str(int(para.space_before.twips)))
        if para.space_after is not None:
            spacing.set(qn("w:after"), str(int(para.space_after.twips)))
        if para.line_spacing is not None:
            spacing.set(qn("w:line"), str(int(para.line_spacing.raw)))
            spacing.set(qn("w:lineRule"), para.line_spacing.rule)

        for run in paragraph.iter(qn("w:r")):
            parent = run.getparent()
            if parent is not None and parent.tag == qn("w:pPr"):
                continue
            rpr = run.find(qn("w:rPr"))
            direct = RunProps.parse(rpr)
            effective = styles.effective_for_run(style_id, direct.style_id, direct)
            if rpr is not None:
                run.remove(rpr)
            rpr = etree.Element(qn("w:rPr"))
            run.insert(0, rpr)
            face = effective.font_ascii or effective.font_hansi
            if face:
                fonts = etree.SubElement(rpr, qn("w:rFonts"))
                for attribute in ("w:ascii", "w:hAnsi", "w:cs"):
                    fonts.set(qn(attribute), face)
            if effective.size is not None:
                etree.SubElement(rpr, qn("w:sz")).set(
                    qn("w:val"), str(int(effective.size.half_points)))
            if effective.bold:
                etree.SubElement(rpr, qn("w:b"))
            if effective.italic:
                etree.SubElement(rpr, qn("w:i"))
            if effective.color:
                etree.SubElement(rpr, qn("w:color")).set(
                    qn("w:val"), effective.color)

    # A real converter does not merely stop using the styles: it stops
    # shipping them. The output has Normal and nothing else.
    styles_part = pkg.related(RT["styles"])
    root = pkg.element(styles_part)
    for element in list(root.findall(qn("w:style"))):
        name = element.find(qn("w:name"))
        value = (name.get(qn("w:val")) or "").lower() if name is not None else ""
        if value not in _KEPT_STYLES:
            root.remove(element)

    pkg.touch(styles_part)
    pkg.touch(pkg.main_document)
    return pkg
