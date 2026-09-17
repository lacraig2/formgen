"""Richer field markers: types, required, and author labels.

The marker grammar is ``[type:] name [*] [| label]``. Beyond text/image/check/
choice there are ``date:`` and ``number:`` (so the form shows the right input);
a trailing ``*`` marks a field required; and a ``| label`` gives the field a
human prompt. For a picture that prompt is also its alt text -- the one piece of
a real picture a bare text marker used to throw away.
"""

from __future__ import annotations

import base64

from fixtures import build
from formgen.content.fill import fill
from formgen.learn.formfields import (
    CHOICE, DATE, NUMBER, TEXT, find_fields, marker_kind,
)
from formgen.opc.ns import qn

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+M8AAAMCAQAY0X2gAAAAAElFTkSuQmCC"
)


# -- grammar --------------------------------------------------------------

def test_date_and_number_are_their_own_kinds():
    assert marker_kind("date: start").kind == DATE
    assert marker_kind("number: salary").kind == NUMBER
    assert marker_kind("amount: total").kind == NUMBER


def test_a_trailing_star_marks_required_and_is_stripped_from_the_name():
    m = marker_kind("email*")
    assert m.kind == TEXT and m.name == "email" and m.required is True
    assert marker_kind("check: agreed*").required is True
    assert marker_kind("date: dob *").name == "dob"  # space before star is fine


def test_a_pipe_gives_a_label_on_every_kind_but_options_on_choice():
    assert marker_kind("full_name | Your full legal name").label == "Your full legal name"
    assert marker_kind("image: logo | Company logo").label == "Company logo"
    # choice keeps the pipe for its options, not a label
    c = marker_kind("choice: status* | Draft, Final")
    assert c.kind == CHOICE and c.required is True
    assert c.choices == ("Draft", "Final") and c.label == ""


def test_required_and_label_compose():
    m = marker_kind("date: start* | Project start date")
    assert m.kind == DATE and m.required and m.name == "start"
    assert m.label == "Project start date"


# -- discovery ------------------------------------------------------------

def test_discovery_surfaces_kind_required_and_label():
    pkg = build.make(body=(
        build.para("Start {{date: start}} pay {{number: salary*}}", style="BodyText")
        + build.para("{{full_name | Your full legal name}}", style="BodyText")))
    fields = {f.name: f for f in find_fields(pkg).fields}
    assert fields["start"].kind == DATE
    assert fields["salary"].kind == NUMBER and fields["salary"].required is True
    assert fields["full_name"].label == "Your full legal name"


# -- filling --------------------------------------------------------------

def test_date_and_number_fill_as_text():
    pkg = build.make(body=build.para(
        "On {{date: start}} for {{number: salary}}", style="BodyText"))
    fill(pkg, {"start": "2026-09-16", "salary": "90000"})
    text = "".join(t.text or "" for t in pkg.element(pkg.main_document).iter(qn("w:t")))
    assert text == "On 2026-09-16 for 90000"


def test_an_image_markers_label_becomes_the_pictures_alt_text():
    pkg = build.make(body=build.para("{{image: logo | Company logo}}", style="BodyText"))
    fill(pkg, {"logo": PNG})
    docprs = list(pkg.element(pkg.main_document).iter(qn("wp:docPr")))
    assert docprs and docprs[0].get("descr") == "Company logo"
    # the picture's own cNvPr carries it too (Word reads either)
    cnv = pkg.element(pkg.main_document).find(".//" + qn("pic:cNvPr"))
    assert cnv is not None and cnv.get("descr") == "Company logo"


def test_required_is_a_form_concern_not_a_fill_one():
    # fill still fills whatever it is given; "required" is enforced by the form,
    # so a required field left unsupplied is simply reported as leftover.
    pkg = build.make(body=build.para("Email: {{email*}}", style="BodyText"))
    report = fill(pkg, {})
    assert report.leftover == ["{{email*}}"]
