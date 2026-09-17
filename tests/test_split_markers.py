"""A marker Word has broken across runs still fills.

Word rarely keeps a typed marker in one run: an edit, a spell-check squiggle,
or bolding half of it splits `{{ref}}` into `{{` / `ref` / `}}`, each its own
run. The fill path pulls such a marker back into a single run before filling it
-- but only when it is going to fill it, so a marker left unfilled stays exactly
as the author wrote it. This is the difference between a value silently going
missing and the document coming out right.
"""

from __future__ import annotations

import base64

from lxml import etree

from fixtures import build
from formgen.content.fill import CHECKED, fill
from formgen.opc.ns import qn
from formgen.safety.verify import check_integrity

# A genuine 1x1 PNG, so the image path parses a real header without needing PIL.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMCAQAY0X2gAAAAAElFTkSuQmCC"
)


def text_of(pkg):
    root = pkg.element(pkg.main_document)
    return "".join(t.text or "" for t in root.iter(qn("w:t")))


def split_runs(*chunks: str, rpr: str = "<w:rPr><w:b/></w:rPr>") -> str:
    """One run per chunk -- how Word stores a marker it has broken up."""
    return "".join(
        f'<w:r>{rpr}<w:t xml:space="preserve">{chunk}</w:t></w:r>'
        for chunk in chunks
    )


def para_of(*chunks: str) -> str:
    return build.para("", runs=split_runs(*chunks))


def blips(pkg):
    return list(pkg.element(pkg.main_document).iter(qn("a:blip")))


# -- text -----------------------------------------------------------------

def test_a_marker_split_three_ways_still_fills():
    pkg = build.make(body=para_of("{{", "ref", "}}"))
    report = fill(pkg, {"ref": "LR-42"})
    assert text_of(pkg) == "LR-42"
    assert report.filled == ["ref"]
    assert check_integrity(pkg) == []


def test_a_marker_split_mid_word_still_fills():
    # A spell-check squiggle splits a run without regard to marker boundaries.
    pkg = build.make(body=para_of("Ref {{re", "fno", "}} end"))
    fill(pkg, {"refno": "X9"})
    assert text_of(pkg) == "Ref X9 end"


def test_the_value_takes_the_first_runs_formatting():
    pkg = build.make(body=para_of("{{", "ref", "}}"))
    fill(pkg, {"ref": "LR-42"})
    root = pkg.element(pkg.main_document)
    holder = next(r for r in root.iter(qn("w:r")) if (r.findtext(qn("w:t")) or ""))
    assert holder.find(qn("w:rPr")).find(qn("w:b")) is not None  # still bold


# -- checkbox and choice --------------------------------------------------

def test_a_checkbox_marker_split_across_runs_still_ticks():
    pkg = build.make(body=para_of("Cleared {{check: ", "ok", "}}"))
    fill(pkg, {"ok": True})
    assert text_of(pkg) == f"Cleared {CHECKED}"


def test_a_choice_marker_split_across_runs_still_picks():
    pkg = build.make(body=para_of("{{choice: s | ", "Draft, Final", "}}"))
    fill(pkg, {"s": "Final"})
    assert text_of(pkg) == "Final"


# -- image ----------------------------------------------------------------

def test_an_image_marker_split_across_runs_still_drops_a_picture():
    pkg = build.make(body=para_of("{{image: ", "photo", "}}"))
    report = fill(pkg, {"photo": PNG})
    assert len(blips(pkg)) == 1
    assert "{{" not in text_of(pkg)
    assert report.filled == ["photo"]
    assert check_integrity(pkg) == []


# -- repeating rows -------------------------------------------------------

def test_a_dotted_marker_split_across_runs_still_repeats():
    cell = f"<w:tc><w:p>{split_runs('{{items.', 'qty', '}}')}</w:p></w:tc>"
    body = f"<w:tbl><w:tr>{cell}</w:tr></w:tbl>"
    pkg = build.make(body=body)
    fill(pkg, {"items": [{"qty": "5"}, {"qty": "9"}]})
    assert text_of(pkg) == "59"
    assert len(list(pkg.element(pkg.main_document).iter(qn("w:tr")))) == 2


# -- the two things hardening must NOT break ------------------------------

def test_an_unfilled_split_marker_is_left_byte_for_byte():
    # No value supplied: coalescing must not run, so a document that merely
    # contains an unrecognised split marker is not silently rewritten.
    pkg = build.make(body=para_of("{{", "ref", "}}"))
    root = pkg.element(pkg.main_document)
    before = etree.tostring(root)
    fill(pkg, {})
    assert etree.tostring(pkg.element(pkg.main_document)) == before


def test_a_value_that_looks_like_a_marker_is_not_re_substituted():
    # Filling `{{a}}` with "«b»" must not then fill «b» from `b` -- the value is
    # data, not a template. Old sequential substitution had exactly this bug.
    pkg = build.make(body=build.para("{{a}}", style="BodyText"))
    report = fill(pkg, {"a": "«b»", "b": "LEAK"})
    assert text_of(pkg) == "«b»"
    assert "LEAK" not in text_of(pkg)
    assert "b" in report.unknown  # supplied but never consumed
