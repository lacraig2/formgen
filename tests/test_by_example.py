"""Learning a template from filled examples.

Show formgen two or more filled copies of the same form; it finds the fields by
what varies between them, names them from the labels alongside, and writes a
marked-up template -- so a normal person authors by pointing at examples instead
of typing marker syntax.
"""

from __future__ import annotations


import pytest

from fixtures import build
from formgen import bench
from formgen.content.session import SESSION_PART
from formgen.learn.byexample import learn_template
from formgen.opc.ns import qn
from formgen.safety.verify import check_integrity


def blank(body: str) -> bytes:
    return bench._save(build.make(body=body))


def filled(template: bytes, **values) -> bytes:
    text = {k: v for k, v in values.items() if not isinstance(v, bool)}
    checks = {k: v for k, v in values.items() if isinstance(v, bool)}
    doc, _ = bench.fill_document(template, text=text, checks=checks)
    return doc


def text_of(data: bytes) -> str:
    pkg = bench._open(data)
    return "".join(t.text or "" for t in pkg.element(pkg.main_document).iter(qn("w:t")))


def learned_fields(docs):
    template, info = learn_template(docs)
    return template, {f["name"]: f["kind"] for f in info["fields"]}


# -- the basics -----------------------------------------------------------

def test_two_copies_reveal_a_field_named_from_its_label():
    tmpl = blank(build.para("Name: {{full_name}}", style="BodyText"))
    a = filled(tmpl, full_name="K. Ito")
    b = filled(tmpl, full_name="J. Doe")
    template, fields = learned_fields([a, b])
    assert fields == {"name": "text"}                       # named from "Name:"
    assert "{{name}}" in text_of(template)


def test_it_needs_at_least_two_copies():
    with pytest.raises(ValueError):
        learn_template([blank(build.para("x", style="BodyText"))])


# -- several fields, several kinds ----------------------------------------

def test_multiple_fields_on_one_line_stay_separate():
    # Three copies -- the recommended count -- so values share little by chance.
    body = build.para("Name: {{n}}   DOB: {{dob}}", style="BodyText")
    tmpl = blank(body)
    a = filled(tmpl, n="K. Ito", dob="1990-05-01")
    b = filled(tmpl, n="J. Doe", dob="1985-11-20")
    c = filled(tmpl, n="A. Ng", dob="2000-01-15")
    _, fields = learned_fields([a, b, c])
    assert list(fields) == ["name", "dob"]                  # reading order kept
    assert fields["dob"] == "date"                          # date value inferred


def test_a_numeric_field_is_typed_as_a_number():
    tmpl = blank(build.para("Salary: {{pay}}", style="BodyText"))
    a, b = filled(tmpl, pay="90000"), filled(tmpl, pay="72000")
    _, fields = learned_fields([a, b])
    assert fields == {"salary": "number"}


def test_a_ticked_box_is_recovered_as_a_checkbox():
    tmpl = blank(build.para("Agreed: {{check: agreed}}", style="BodyText"))
    a, b = filled(tmpl, agreed=True), filled(tmpl, agreed=False)
    _, fields = learned_fields([a, b])
    assert fields == {"agreed": "checkbox"}


# -- the output is a real, clean template ---------------------------------

def test_the_learned_template_is_clean_and_fillable():
    tmpl = blank(build.para("Client: {{who}}", style="BodyText"))
    a = filled(tmpl, who="ACME Ltd")
    b = filled(tmpl, who="Globex")
    template, info = learn_template([a, b])
    pkg = bench._open(template)
    assert SESSION_PART not in pkg                          # no donor's session
    assert check_integrity(pkg) == []
    # round-trips through the normal fill path
    out, report = bench.fill_document(template, text={"client": "Initech"})
    assert "Initech" in text_of(out)
    assert report["leftover"] == []
    assert info["samples"] == 2


def test_unchanged_documents_yield_no_fields():
    same = blank(build.para("Static form, no blanks.", style="BodyText"))
    _, info = learn_template([same, same])
    assert info["fields"] == []
