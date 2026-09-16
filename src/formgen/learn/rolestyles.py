"""Writing the learned format into the donor as real styles.

A donor cloned from a hand-formatted exemplar carries no styles worth the
name -- every paragraph in it is `Normal`, because that is what Google Docs
export, PDF conversion and plain manual formatting produce, and it is the
state 48% of real documents are in. The role ballot recovers what the
headings look like in such a corpus, but recovering it into `profile.json`
is only half a profile: `apply` restyles a paragraph by pointing `w:pStyle`
at a style, and `new` emits headings the same way. With no style to point
at, a format that is known cannot be applied.

So the roles the corpus agreed on are materialized as styles in the donor.
This is also the answer to a trap that predates the hand-formatted case: a
`w:pStyle` referring to a style the document does not define falls back to
Word's *built-in* definition of that name, which differs by Word version and
by locale. A profile that says "Heading 1" without defining it is not
specifying a format, it is naming one and hoping.

Only roles the corpus was confident about are written, and only where the
donor does not already define them. A style the donor has is the real
article, carrying conditional formatting, latent-style flags and linked
character styles that no reconstruction from JSON could reproduce.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import RT, qn
from ..opc.package import OpcPackage
from ..oox.styles import StyleGraph, normalize_style_name
from ..oox.values import FontSize, Length, LineSpacing

# The Word name each role is written under, chosen so that `role_for_style_name`
# reads it back as the same role -- the mapping has to round-trip or a
# re-learn would classify our own donor differently from the corpus.
ROLE_STYLE_NAME = {
    "title": "Title",
    "subtitle": "Subtitle",
    "body": "Body Text",
    "caption": "Caption",
    "quote": "Quote",
    "list_bullet": "List Bullet",
    "list_number": "List Number",
}

# w:pPr is a sequence; these are the children we write, in schema order.
_PPR_ORDER = ("w:keepNext", "w:keepLines", "w:pageBreakBefore", "w:widowControl",
              "w:spacing", "w:ind", "w:contextualSpacing", "w:jc", "w:outlineLvl")


@dataclass
class RoleStyleReport:
    added: list[str] = field(default_factory=list)
    already_defined: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    # role -> the style name in the donor that now implements it. Only roles
    # whose style is really there: this is what `apply` points w:pStyle at.
    mapping: dict[str, str] = field(default_factory=dict)

    def note(self) -> str | None:
        if not self.added:
            return None
        return (
            f"the exemplars carry no styles, so the format was read from how "
            f"they look; {len(self.added)} style(s) were written into "
            f"template.docx to hold it ({', '.join(self.added[:4])}"
            + (", ..." if len(self.added) > 4 else "")
            + "). Check them in Word's Styles pane before relying on them."
        )


def style_name_for(role: str) -> str | None:
    if role in ROLE_STYLE_NAME:
        return ROLE_STYLE_NAME[role]
    match = re.fullmatch(r"heading(\d)", role)
    return f"Heading {match.group(1)}" if match else None


def _style_id(name: str) -> str:
    """A styleId Word will accept: letters and digits only."""
    return re.sub(r"[^A-Za-z0-9]", "", name) or "FormgenStyle"


def _set(parent: etree._Element, tag: str, **attrs) -> etree._Element:
    element = etree.SubElement(parent, qn(tag))
    for key, value in attrs.items():
        element.set(qn(key.replace("_", ":")), value)
    return element


def _place(ppr: etree._Element, tag: str, **attrs) -> None:
    element = etree.Element(qn(tag))
    for key, value in attrs.items():
        element.set(qn(key.replace("_", ":")), value)
    index = _PPR_ORDER.index(tag)
    for existing in ppr:
        if not isinstance(existing.tag, str):
            continue
        name = existing.tag.split("}")[-1]
        spelled = f"w:{name}"
        if spelled in _PPR_ORDER and _PPR_ORDER.index(spelled) > index:
            existing.addprevious(element)
            return
    ppr.append(element)


def _run_props(rpr: etree._Element, values: dict) -> None:
    font = values.get("font_ascii")
    if font:
        _set(rpr, "w:rFonts", w_ascii=font, w_hAnsi=font, w_cs=font)
    for prop, tag in (("bold", "w:b"), ("italic", "w:i"),
                      ("caps", "w:caps"), ("small_caps", "w:smallCaps")):
        value = values.get(prop)
        if value is not None:
            # Always explicit: a bare <w:b/> is a toggle, and a toggle
            # inherited into a context that already had it turns itself off.
            _set(rpr, tag, w_val="1" if value else "0")
    color = values.get("color")
    if color:
        _set(rpr, "w:color", w_val=color)
    size = values.get("size")
    if isinstance(size, FontSize):
        _set(rpr, "w:sz", w_val=str(int(size.half_points)))
        _set(rpr, "w:szCs", w_val=str(int(size.half_points)))


def _para_props(ppr: etree._Element, values: dict, outline: int | None) -> None:
    spacing: dict[str, str] = {}
    before, after = values.get("space_before"), values.get("space_after")
    if isinstance(before, Length):
        spacing["w_before"] = str(int(before.twips))
    if isinstance(after, Length):
        spacing["w_after"] = str(int(after.twips))
    line = values.get("line_spacing")
    if isinstance(line, LineSpacing):
        spacing["w_line"] = str(int(line.raw))
        spacing["w_lineRule"] = line.rule
    if spacing:
        _place(ppr, "w:spacing", **spacing)

    indent: dict[str, str] = {}
    for prop, attr in (("indent_left", "w_left"), ("indent_right", "w_right"),
                       ("indent_first_line", "w_firstLine"),
                       ("indent_hanging", "w_hanging")):
        value = values.get(prop)
        if isinstance(value, Length) and value.twips:
            indent[attr] = str(int(value.twips))
    if indent:
        _place(ppr, "w:ind", **indent)

    alignment = values.get("alignment")
    if alignment:
        _place(ppr, "w:jc", w_val=alignment)
    for prop, tag in (("keep_next", "w:keepNext"), ("keep_lines", "w:keepLines"),
                      ("page_break_before", "w:pageBreakBefore"),
                      ("contextual_spacing", "w:contextualSpacing")):
        if values.get(prop):
            _place(ppr, tag, w_val="1")
    if outline is not None:
        _place(ppr, "w:outlineLvl", w_val=str(outline))


def materialize_roles(pkg: OpcPackage, values: dict, roles: set[str] | None = None,
                      ) -> RoleStyleReport:
    """Write a style for every role the corpus agreed on but the donor lacks.

    `values` is the consensus keyed by JSON Pointer -- the same mapping the
    profile is written from, so what lands in the donor and what lands in
    profile.json cannot disagree.
    """
    report = RoleStyleReport()
    part = pkg.related(RT["styles"])
    if not part or part not in pkg:
        report.skipped.append(("*", "the donor has no styles part"))
        return report
    root = pkg.element(part)
    graph = StyleGraph.parse(root)
    defined = {normalize_style_name(s.name) for s in graph.styles.values()}
    existing_ids = set(graph.styles)

    wanted: dict[str, dict] = {}
    for pointer, value in values.items():
        if not pointer.startswith("/roles/"):
            continue
        parts = pointer.split("/")
        if len(parts) != 5:
            continue
        _, _, role, group, prop = parts
        if roles is not None and role not in roles:
            continue
        wanted.setdefault(role, {}).setdefault(group, {})[prop] = value

    for role in sorted(wanted):
        name = style_name_for(role)
        if name is None:
            report.skipped.append((role, "no Word style name corresponds"))
            continue
        if normalize_style_name(name) in defined:
            report.already_defined.append(name)
            report.mapping[role] = name
            continue
        style_id = _style_id(name)
        if style_id in existing_ids:
            report.skipped.append((role, f"styleId {style_id} is taken"))
            continue
        _append_style(root, role, name, style_id, wanted[role])
        existing_ids.add(style_id)
        defined.add(normalize_style_name(name))
        report.added.append(name)
        report.mapping[role] = name

    if report.added:
        pkg.touch(part)
    return report


def _append_style(root: etree._Element, role: str, name: str, style_id: str,
                  groups: dict) -> None:
    style = etree.SubElement(root, qn("w:style"))
    style.set(qn("w:type"), "paragraph")
    style.set(qn("w:styleId"), style_id)
    _set(style, "w:name", w_val=name)
    # basedOn Normal, so anything the corpus had no opinion about still
    # inherits the document's own defaults rather than Word's.
    _set(style, "w:basedOn", w_val="Normal")
    _set(style, "w:qFormat")

    match = re.fullmatch(r"heading(\d)", role)
    outline = int(match.group(1)) - 1 if match else None
    ppr = etree.SubElement(style, qn("w:pPr"))
    _para_props(ppr, groups.get("para", {}), outline)
    if len(ppr) == 0:
        style.remove(ppr)
    rpr = etree.SubElement(style, qn("w:rPr"))
    _run_props(rpr, groups.get("run", {}))
    if len(rpr) == 0:
        style.remove(rpr)
