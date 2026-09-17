"""doctor's comparison logic, driven with fake Word readings.

The COM plumbing cannot run here, so what is tested is the part that decides
whether Word and our resolver disagree -- which is also the part with the
interesting bugs.
"""

from __future__ import annotations


import pytest

from fixtures import build
from formgen.oox.props import ParaProps, RunProps
from formgen.oox.values import FontSize, Length, LineSpacing
from formgen.word import com
from formgen.word.doctor import (
    WD_UNDEFINED, OurParagraph, WordParagraph, compare_paragraph,
    compare_streams, read_our_paragraphs, run,
)


def ours(text="x", **kwargs):
    run_keys = {"size", "font_ascii", "font_ascii_theme", "bold", "italic"}
    return OurParagraph(
        text=text,
        run=RunProps(**{k: v for k, v in kwargs.items() if k in run_keys}),
        para=ParaProps(**{k: v for k, v in kwargs.items() if k not in run_keys}),
    )


def theirs(text="x", **kwargs):
    return WordParagraph(text=text, **kwargs)


# -- agreement ------------------------------------------------------------


def test_matching_formatting_produces_no_differences():
    assert compare_paragraph(
        ours(size=FontSize.pt(11), bold=True, space_after=Length.pt(6),
             alignment="both"),
        theirs(font_size=11.0, bold=-1, space_after=6.0, alignment=3),
        "p1",
    ) == []


def test_word_uses_minus_one_for_true():
    assert compare_paragraph(ours(bold=True), theirs(bold=-1), "p1") == []
    assert compare_paragraph(ours(bold=False), theirs(bold=0), "p1") == []


def test_a_half_twip_of_float_slop_is_not_a_difference():
    assert compare_paragraph(
        ours(space_after=Length.pt(6)), theirs(space_after=6.05), "p1"
    ) == []


# -- disagreement ---------------------------------------------------------


def test_a_real_size_difference_is_reported_with_both_answers():
    diffs = compare_paragraph(
        ours(size=FontSize.pt(11)), theirs(font_size=13.0), "paragraph 4"
    )
    assert len(diffs) == 1
    assert diffs[0].describe() == (
        "paragraph 4: font size  we say 11pt, Word says 13pt"
    )


def test_a_toggle_resolved_wrongly_is_caught():
    """The whole point of doctor: our toggle model is Word-parity or it is not."""
    diffs = compare_paragraph(ours(bold=True), theirs(bold=0), "p1")
    assert [d.property for d in diffs] == ["bold"]


def test_an_outline_level_is_compared_on_words_one_based_scale():
    assert compare_paragraph(
        ours(outline_level=0), theirs(outline_level=1), "p1"
    ) == []
    diffs = compare_paragraph(ours(outline_level=0), theirs(outline_level=3), "p1")
    assert [d.property for d in diffs] == ["outline level"]


def test_body_text_outline_level_is_word_level_ten():
    assert compare_paragraph(
        ours(outline_level=9), theirs(outline_level=10), "p1"
    ) == []


# -- things Word cannot answer -------------------------------------------


def test_a_mixed_run_is_skipped_rather_than_reported():
    """Word returns 9999999 for "not uniform", which our model cannot hold."""
    assert compare_paragraph(
        ours(size=FontSize.pt(11), bold=True),
        theirs(font_size=WD_UNDEFINED, bold=WD_UNDEFINED),
        "p1",
    ) == []


def test_a_themed_font_is_not_asserted_about():
    """Theme indirection is an open question, not a bug we can claim here."""
    assert compare_paragraph(
        ours(font_ascii="Calibri", font_ascii_theme="minorHAnsi"),
        theirs(font_name="Aptos"),
        "p1",
    ) == []
    diffs = compare_paragraph(
        ours(font_ascii="Calibri"), theirs(font_name="Aptos"), "p1"
    )
    assert [d.property for d in diffs] == ["font"]


# -- line spacing, which Word spells three different ways -----------------

@pytest.mark.parametrize("multiple,rule,value", [
    (1.0, 0, 12.0),     # wdLineSpaceSingle
    (1.5, 1, 18.0),     # wdLineSpace1pt5
    (2.0, 2, 24.0),     # wdLineSpaceDouble
    (1.15, 5, 13.8),    # wdLineSpaceMultiple
])
def test_auto_line_spacing_matches_whichever_spelling_word_used(multiple, rule, value):
    assert compare_paragraph(
        ours(line_spacing=LineSpacing("auto", round(multiple * 240))),
        theirs(line_spacing_rule=rule, line_spacing=value),
        "p1",
    ) == []


def test_exact_line_spacing_must_not_match_an_auto_multiple():
    """240 is single spacing at auto and 12pt at exact; conflating them is a
    10x error that looks like agreement."""
    diffs = compare_paragraph(
        ours(line_spacing=LineSpacing("exact", 240)),
        theirs(line_spacing_rule=0, line_spacing=12.0),
        "p1",
    )
    assert [d.property for d in diffs] == ["line spacing"]


def test_exact_line_spacing_agrees_when_word_says_exact():
    assert compare_paragraph(
        ours(line_spacing=LineSpacing("exact", 240)),
        theirs(line_spacing_rule=4, line_spacing=12.0),
        "p1",
    ) == []


# -- stream alignment -----------------------------------------------------


def test_streams_are_aligned_on_text_before_formatting_is_compared():
    mine = [ours("Introduction", size=FontSize.pt(16)), ours("Body", size=FontSize.pt(11))]
    yours = [theirs("Introduction", font_size=16.0), theirs("Body", font_size=11.0)]
    diffs, note = compare_streams(mine, yours)
    assert diffs == [] and note == ""


def test_a_divergent_stream_stops_rather_than_reporting_a_hundred_false_diffs():
    mine = [ours("A", size=FontSize.pt(11)), ours("B", size=FontSize.pt(11))]
    yours = [theirs("A", font_size=11.0), theirs("SOMETHING ELSE", font_size=99.0)]
    diffs, note = compare_streams(mine, yours)
    assert diffs == []
    assert "diverge at paragraph 2" in note


def test_a_length_mismatch_is_noted_and_the_overlap_still_compared():
    mine = [ours("A", size=FontSize.pt(11)), ours("B")]
    yours = [theirs("A", font_size=13.0)]
    diffs, note = compare_streams(mine, yours)
    assert [d.property for d in diffs] == ["font size"]
    assert "we found 2 paragraphs and Word found 1" in note


# -- our side of the comparison ------------------------------------------


def test_our_paragraph_reader_resolves_through_the_style_chain():
    paragraphs = read_our_paragraphs(build.make())
    assert [p.text for p in paragraphs] == [
        "A Fixture Document", "Introduction", "The body text of the document.",
    ]
    assert paragraphs[0].run.size == FontSize.pt(28)     # Title
    assert paragraphs[2].run.size == FontSize.pt(11)     # docDefaults
    assert paragraphs[1].para.outline_level == 0


def test_text_boxes_are_excluded_because_word_puts_them_in_another_story():
    body = build.para("body", style="BodyText") + (
        "<w:p><w:r><w:pict><v:shape xmlns:v='urn:schemas-microsoft-com:vml'>"
        "<v:textbox><w:txbxContent><w:p><w:r><w:t>in a box</w:t></w:r></w:p>"
        "</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>"
    )
    texts = [p.text for p in read_our_paragraphs(build.make(body=body))]
    assert "in a box" not in texts


# -- degradation ----------------------------------------------------------


# The three below assert the *Word-absent* path -- what a machine without Word
# automation does. On a Windows CI runner with pywin32 and a desktop session
# `com.available()` optimistically says yes, so they only make sense where it
# says no (Linux, and bare Windows without pywin32).
_word_absent = pytest.mark.skipif(
    com.available()[0],
    reason="this machine reports Word automation available; these assert the "
           "Word-absent path",
)


@_word_absent
def test_doctor_skips_cleanly_where_word_is_absent(tmp_path):
    report = run(tmp_path / "missing.docx")
    assert report.skipped is True
    assert report.ok is True          # a skip is never a failure
    assert "works without Word" in report.render()


@_word_absent
def test_availability_reports_a_reason_a_human_can_act_on():
    usable, reason = com.available()
    assert usable is False
    assert reason and reason[0].isupper()


@_word_absent
def test_the_cli_doctor_command_skips_without_failing(tmp_path):
    from click.testing import CliRunner
    from formgen.cli import cli

    path = tmp_path / "one.docx"
    build.make().save(path, deterministic=True)
    result = CliRunner().invoke(cli, ["doctor", str(path)], obj={})
    assert result.exit_code == 0
    assert "SKIPPED" in result.output
