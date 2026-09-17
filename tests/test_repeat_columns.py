"""Repeating rows carry the same richness a single field does.

A dotted marker names a column of a repeating group. That column can be any
kind: `{{items.qty}}` is text, `{{image: items.photo}}` a picture, `{{check:
items.done}}` a tick box, `{{choice: items.grade | A, B}}` a pick list. Each
record fills one row, so a table of photos or ticks -- a very ordinary thing to
want -- comes out right instead of shipping literal marker text.
"""

from __future__ import annotations

import base64

from fixtures import build
from formgen.content.fill import CHECKED, UNCHECKED, fill
from formgen.learn.formfields import CHECKBOX, CHOICE, IMAGE, TEXT, find_fields, find_repeats
from formgen.opc.ns import qn
from formgen.safety.verify import check_integrity

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMCAQAY0X2gAAAAAElFTkSuQmCC"
)


def cell(marker: str) -> str:
    return (f'<w:tc><w:p><w:r><w:t xml:space="preserve">{marker}</w:t>'
            "</w:r></w:p></w:tc>")


def rich_table() -> str:
    return (
        "<w:tbl><w:tr>"
        + cell("{{items.material}}")
        + cell("{{image: items.photo}}")
        + cell("{{check: items.done}}")
        + cell("{{choice: items.grade | A, B, C}}")
        + "</w:tr></w:tbl>"
    )


def text_of(pkg):
    root = pkg.element(pkg.main_document)
    return "".join(t.text or "" for t in root.iter(qn("w:t")))


# -- discovery ------------------------------------------------------------

def test_a_rich_column_is_discovered_with_its_kind():
    pkg = build.make(body=rich_table())
    groups = find_repeats(pkg)
    assert len(groups) == 1
    kinds = {c.name: c.kind for c in groups[0].columns}
    assert kinds == {"material": TEXT, "photo": IMAGE,
                     "done": CHECKBOX, "grade": CHOICE}
    grade = next(c for c in groups[0].columns if c.name == "grade")
    assert grade.choices == ("A", "B", "C")


def test_a_rich_column_is_not_also_a_top_level_field():
    # The bug this fixes: image/check/choice dotted markers leaking out as
    # detached single fields (items_photo, items_done) unconnected to the rows.
    pkg = build.make(body=rich_table())
    names = {f.name for f in find_fields(pkg).fields}
    assert not any(n.startswith("items") for n in names)


# -- filling --------------------------------------------------------------

def test_each_record_fills_one_row_of_every_kind():
    pkg = build.make(body=rich_table())
    report = fill(pkg, {"items": [
        {"material": "Cu", "photo": PNG, "done": True, "grade": "A"},
        {"material": "Al", "photo": PNG, "done": False, "grade": "C"},
    ]})
    root = pkg.element(pkg.main_document)
    assert text_of(pkg) == f"Cu{CHECKED}AAl{UNCHECKED}C"
    assert len(list(root.iter(qn("a:blip")))) == 2   # one picture per row
    assert len(list(root.iter(qn("w:tr")))) == 2
    assert report.filled == ["items"]
    assert report.leftover == []
    assert check_integrity(pkg) == []


def test_a_missing_column_in_a_record_clears_not_crashes():
    pkg = build.make(body=rich_table())
    fill(pkg, {"items": [{"material": "Cu"}]})   # no photo/done/grade
    # text cleared, box unticked, no picture, no leftover marker text
    assert text_of(pkg) == f"Cu{UNCHECKED}"
    assert list(pkg.element(pkg.main_document).iter(qn("a:blip"))) == []
    assert check_integrity(pkg) == []


# -- the safety net -------------------------------------------------------

def test_an_unfilled_marker_is_reported_not_shipped_silently():
    pkg = build.make(body=build.para("Hello {{missing}} world", style="BodyText"))
    report = fill(pkg, {})
    assert report.leftover == ["{{missing}}"]


def test_a_filled_marker_leaves_nothing_behind():
    pkg = build.make(body=build.para("Name: {{name}}", style="BodyText"))
    report = fill(pkg, {"name": "Ito"})
    assert report.leftover == []
