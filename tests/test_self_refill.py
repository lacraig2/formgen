"""A filled document that carries its own blank form and last answers.

`bench.fill_document` tucks the original template and the answers into a part
Word ignores, so reopening a finished document rebuilds the form pre-filled and
a changed answer re-fills from the clean template rather than from filled text.
"""

from __future__ import annotations

import base64

from fixtures import build
from formgen import bench
from formgen.content.session import (
    SESSION_PART, encode_answers, read_session, write_session,
)
from formgen.opc.ns import qn
from formgen.safety.verify import check_integrity


def blank(marker_body: str) -> bytes:
    return bench._save(build.make(body=marker_body))


def text_of(data: bytes) -> str:
    pkg = bench._open(data)
    return "".join(t.text or "" for t in pkg.element(pkg.main_document).iter(qn("w:t")))


# -- the part -------------------------------------------------------------

def test_write_then_read_session_round_trips():
    pkg = build.make(body=build.para("hi", style="BodyText"))
    write_session(pkg, b"TEMPLATE-BYTES", {"text": {"a": "b"}})
    again = bench._open(bench._save(pkg))
    session = read_session(again)
    assert session is not None
    assert base64.b64decode(session["template"]) == b"TEMPLATE-BYTES"
    assert session["answers"] == {"text": {"a": "b"}}


def test_a_plain_document_has_no_session():
    assert read_session(bench._open(blank(build.para("{{x}}", style="BodyText")))) is None
    assert "prefill" not in bench.inspect(blank(build.para("{{x}}", style="BodyText")))


# -- the round trip -------------------------------------------------------

def test_a_filled_document_carries_its_form_and_answers():
    tmpl = blank(build.para("Name: {{full_name}} agree {{check: ok}}", style="BodyText"))
    filled, _ = bench.fill_document(tmpl, text={"full_name": "K. Ito"}, checks={"ok": True})
    assert SESSION_PART in bench._open(filled)

    reopened = bench.inspect(filled)
    assert [f["name"] for f in reopened["fields"]] == ["full_name", "ok"]
    assert reopened["prefill"]["text"]["full_name"] == "K. Ito"
    assert reopened["prefill"]["checks"]["ok"] is True
    assert check_integrity(bench._open(filled)) == []


def test_re_filling_a_filled_document_starts_from_the_clean_template():
    tmpl = blank(build.para("Name: {{full_name}}", style="BodyText"))
    once, _ = bench.fill_document(tmpl, text={"full_name": "K. Ito"})
    assert "K. Ito" in text_of(once)

    # Reopen the FILLED doc and change the answer -- it must fill from the
    # embedded template, not double-substitute the already-filled text.
    twice, _ = bench.fill_document(once, text={"full_name": "J. Doe"})
    assert "J. Doe" in text_of(twice)
    assert "K. Ito" not in text_of(twice)
    assert bench.inspect(twice)["prefill"]["text"]["full_name"] == "J. Doe"
    assert SESSION_PART in bench._open(twice)   # still self-refilling


# -- answer encoding ------------------------------------------------------

def test_encode_answers_makes_images_json_safe():
    answers = encode_answers(
        {"note": "hi"}, {"ok": True}, {"logo": b"\x89PNGdata"},
        {"items": [{"qty": "2", "photo": b"\x89PNGrow"}]})
    assert answers["text"] == {"note": "hi"}
    assert answers["checks"] == {"ok": True}
    assert answers["images"]["logo"] == base64.b64encode(b"\x89PNGdata").decode()
    row = answers["groups"]["items"][0]
    assert row["qty"] == "2"
    assert row["photo"] == {"image": base64.b64encode(b"\x89PNGrow").decode()}
