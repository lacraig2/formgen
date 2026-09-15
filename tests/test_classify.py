"""Classification: an evidence vote, not a cascade."""

from __future__ import annotations

import pytest

from fixtures import build
from formgen.classify.features import build_context
from formgen.classify.rules import (
    BODY, CAPTION, LIST_BULLET, LIST_NUMBER, PLACEHOLDER, TITLE,
    classify_block, classify_document, role_for_style_name,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def direct(text, sz=22, bold=False, keep_next=False, **kw):
    rpr = f'<w:sz w:val="{sz}"/>' + ("<w:b/>" if bold else "")
    ppr = "<w:keepNext/>" if keep_next else ""
    return build.para(text, rpr=rpr, ppr_extra=ppr, **kw)


def classify(body, styles=None, **kwargs):
    ctx = build_context(build.make(body=body, styles=styles))
    return ctx, classify_document(ctx, **kwargs)


def ordered(ctx, results):
    return [
        (results[f.path].role, results[f.path].confidence, f.text)
        for f in ctx.features if f.is_paragraph
    ]


# -- the styled path ------------------------------------------------------


def test_a_styled_document_classifies_from_its_styles():
    ctx, results = classify(
        build.para("A Title", style="Title")
        + build.para("Introduction", style="Heading1")
        + build.para("Some prose about the thing.", style="BodyText"),
        known_styles={"title", "heading 1", "body text"},
    )
    assert [r for r, _, _ in ordered(ctx, results)] == ["title", "heading1", "body"]
    assert all(c > 0.9 for _, c, _ in ordered(ctx, results))


def test_outline_level_beats_a_missing_style_name():
    """Resolved w:outlineLvl is the strongest heading signal after the style."""
    styles = list(build.DEFAULT_STYLES) + [
        build.style("Sec", "Section Header", outline=1, rpr='<w:sz w:val="26"/>')
    ]
    ctx, results = classify(build.para("Scope", style="Sec"), styles=styles)
    assert results["body/p[1]"].role == "heading2"


@pytest.mark.parametrize("name,role", [
    ("heading 3", "heading3"),
    ("Title", "title"),
    ("caption", "caption"),
    ("body text", "body"),
    ("List Bullet 2", "list_bullet"),
    ("Intense Quote", "quote"),
    ("something bespoke", None),
])
def test_style_names_map_to_roles_case_insensitively(name, role):
    from formgen.oox.styles import normalize_style_name

    assert role_for_style_name(normalize_style_name(name)) == role


# -- the converted-document path -----------------------------------------


def test_an_entirely_unstyled_document_still_gets_a_hierarchy():
    """Google Docs and PDF conversions put Normal on every paragraph, so a
    cascade that starts at w:pStyle never fires at all."""
    body = (
        direct("Thermal Margin Analysis of the X-7", 56, bold=True)
        + direct("Introduction", 32, bold=True, keep_next=True)
        + direct("The radiator exceeds its design margin under load everywhere.")
        + direct("Test Setup", 26, bold=True, keep_next=True)
        + direct("The panel was soaked at 340 K for six hours before measuring.")
        + direct("Instrumentation", 26, bold=True, keep_next=True)
        + direct("Thermocouples were bonded at nine stations across the face.")
    )
    ctx, results = classify(body)
    roles = [r for r, _, _ in ordered(ctx, results)]
    assert roles == [
        "title", "heading1", "body", "heading2", "body", "heading2", "body",
    ]


def test_the_default_style_is_not_evidence_when_everything_has_it():
    """Being Normal in a document that is entirely Normal says nothing --
    and letting it win classifies the title as body text."""
    body = direct("A Very Large Title Indeed", 56, bold=True) + direct(
        "Ordinary prose that runs on for a while and ends properly."
    )
    ctx, results = classify(body)
    assert results["body/p[1]"].role == TITLE


def test_heading_levels_come_from_ranking_the_documents_own_formats():
    body = (
        direct("Big", 32, bold=True, keep_next=True)
        + direct("Prose that is long enough to count as body text here.")
        + direct("Small", 24, bold=True, keep_next=True)
        + direct("More prose that is long enough to count as body text.")
        + direct("Bigger", 40, bold=True, keep_next=True)
        + direct("Yet more prose that is long enough to count as body text.")
    )
    ctx, results = classify(body)
    got = {text: role for role, _, text in ordered(ctx, results)}
    assert got["Bigger"] == "heading1"
    assert got["Big"] == "heading2"
    assert got["Small"] == "heading3"


def test_a_heading_level_never_skips_downward():
    styles = list(build.DEFAULT_STYLES) + [
        build.style("H4", "heading 4", outline=3, rpr='<w:sz w:val="24"/>')
    ]
    ctx, results = classify(
        build.para("Top", style="Heading1") + build.para("Way down", style="H4"),
        styles=styles,
    )
    assert results["body/p[2]"].role == "heading2"
    assert any("skip" in e for e in results["body/p[2]"].evidence)


# -- lists ----------------------------------------------------------------


def test_a_numbered_paragraph_is_a_list_whatever_its_style():
    ctx, results = classify(build.para("First item", numid=2))
    assert results["body/p[1]"].role == LIST_NUMBER


def test_a_bulleted_paragraph_is_distinguished_from_a_numbered_one():
    ctx, results = classify(build.para("A bullet", numid=1))
    assert results["body/p[1]"].role == LIST_BULLET


def test_numid_zero_does_not_make_a_paragraph_a_list():
    body = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="0"/>'
            "</w:numPr></w:pPr><w:r><w:t>Not a list item at all, just prose."
            "</w:t></w:r></w:p>")
    ctx, results = classify(body)
    assert results["body/p[1]"].role != LIST_NUMBER


# -- captions -------------------------------------------------------------


def test_a_seq_field_makes_a_caption_even_across_split_runs():
    """Word splits field instructions mid-word, so run-by-run matching finds
    nothing in exactly the documents that have real auto-numbered captions."""
    body = (
        "<w:p><w:r><w:t>Figure </w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> SEQ Fig</w:instrText></w:r>'
        '<w:r><w:instrText xml:space="preserve">ure \\* ARABIC </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>3</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "<w:r><w:t>. The test article.</w:t></w:r></w:p>"
    )
    ctx, results = classify(body)
    assert results["body/p[1]"].role == CAPTION
    assert any("SEQ Figure" in e for e in results["body/p[1]"].evidence)


def test_a_caption_is_recognised_next_to_a_figure():
    body = (
        "<w:p><w:r><w:drawing/></w:r></w:p>"
        + direct("Figure 3. The test article in the chamber.", 18)
    )
    ctx, results = classify(body)
    assert results["body/p[2]"].role == CAPTION


def test_caption_text_alone_is_not_enough_without_a_figure():
    ctx, results = classify(direct("Table stakes for the project are high."))
    assert results["body/p[1]"].role != CAPTION


# -- content controls -----------------------------------------------------


def test_a_tagged_content_control_short_circuits_everything():
    """The document declaring "this is the report number" beats any heuristic."""
    body = (
        f'<w:sdt xmlns:w="{W_NS}"><w:sdtPr>'
        '<w:tag w:val="formgen.report_number"/></w:sdtPr><w:sdtContent>'
        + direct("LR-2026-0142", 56, bold=True)
        + "</w:sdtContent></w:sdt>"
    )
    ctx, results = classify(body, placeholders={"report_number"})
    role = next(iter(results.values())).role
    assert role == PLACEHOLDER


def test_an_unrelated_content_control_is_classified_normally():
    body = (
        f'<w:sdt xmlns:w="{W_NS}"><w:sdtPr><w:tag w:val="CoverDate"/>'
        "</w:sdtPr><w:sdtContent>"
        + direct("Ordinary prose inside someone else's control here.")
        + "</w:sdtContent></w:sdt>"
    )
    ctx, results = classify(body, placeholders={"report_number"})
    assert next(iter(results.values())).role == BODY


# -- consistency ----------------------------------------------------------


def test_identically_formatted_paragraphs_get_the_same_role():
    """One paragraph in twenty loses a close vote for an accidental reason."""
    prose = "".join(
        direct(f"Paragraph {i} of prose that ends with a full stop.") for i in range(5)
    )
    # One of them lacks the terminal punctuation that the others have.
    odd = direct("Paragraph five of prose that ends without one")
    ctx, results = classify(prose + odd)
    roles = {results[f.path].role for f in ctx.features if f.is_paragraph}
    assert roles == {BODY}


def test_a_lone_differing_paragraph_is_allowed_to_differ():
    body = direct("Heading Here", 40, bold=True, keep_next=True) + "".join(
        direct(f"Paragraph {i} of ordinary prose, ending properly.")
        for i in range(4)
    )
    ctx, results = classify(body)
    assert results["body/p[1]"].role in ("title", "heading1")
    assert {results[f.path].role for f in ctx.features[1:]} == {BODY}


def test_only_the_opening_paragraph_can_be_the_title():
    """Size alone cannot tell a title from a big heading; position can."""
    body = (
        direct("Report Title", 56, bold=True)
        + "".join(direct(f"Prose paragraph {i} that ends properly here.")
                 for i in range(4))
        + direct("A Late Big Heading", 56, bold=True, keep_next=True)
        + direct("More prose that ends properly, as prose does.")
    )
    ctx, results = classify(body)
    roles = [r for r, _, _ in ordered(ctx, results)]
    assert roles[0] == TITLE
    assert roles.count(TITLE) == 1
    assert roles[5].startswith("heading")


# -- confidence -----------------------------------------------------------


def test_corroborating_signals_raise_confidence_without_exceeding_one():
    ctx, results = classify(
        build.para("Introduction", style="Heading1"),
        known_styles={"heading 1"},
    )
    assert 0.9 < results["body/p[1]"].confidence <= 1.0


def test_a_paragraph_with_nothing_to_go_on_is_low_confidence():
    ctx = build_context(build.make(body=build.para("x")))
    feature = ctx.features[0]
    result = classify_block(feature, ctx)
    assert result.confidence < 0.9
    assert result.evidence


def test_an_empty_paragraph_is_its_own_role():
    ctx, results = classify("<w:p/>")
    assert results["body/p[1]"].role == "empty"
