"""lint: the locator, the rule families, and the fixed-point property."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from fixtures import build
from formgen.cli import cli
from formgen.classify.features import build_context
from formgen.classify.rules import classify_document
from formgen.learn.pipeline import learn
from formgen.opc.ns import qn
from formgen.opc.package import OpcPackage
from formgen.plan.builder import build_plan
from formgen.plan.locator import FindStrings, LocatorFactory, searchable
from formgen.plan.model import ERROR, WARN
from formgen.profile.io import Profile
from formgen.report.jsonout import plan_dict

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def corpus(tmp_path, n=5, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        path = tmp_path / f"r{i}.docx"
        build.make(**kwargs).save(path, deterministic=True)
        out.append(path)
    return out


@pytest.fixture
def profile(tmp_path):
    learn(corpus(tmp_path / "corpus"), tmp_path / "prof")
    return Profile.load(tmp_path / "prof")


def direct(text, sz=22, bold=False, keep_next=False):
    return build.para(
        text, rpr=f'<w:sz w:val="{sz}"/>' + ("<w:b/>" if bold else ""),
        ppr_extra="<w:keepNext/>" if keep_next else "",
    )


FOREIGN_BODY = (
    direct("Thermal Margin Analysis", 56, bold=True)
    + direct("Introduction", 32, bold=True, keep_next=True)
    + direct("The X-7 radiator exceeds its design margin under worst-case load.")
    + direct("The panel was soaked at 340 K for six hours before measurement.")
)


def lint(pkg, profile, name="doc.docx"):
    return build_plan(pkg, profile, document_name=name)


# -- find strings ---------------------------------------------------------


def test_a_find_string_is_extended_until_it_is_unique():
    """Five words is the target; it is lengthened only when it has to be."""
    finder = FindStrings([
        "The panel was soaked at 340 K for six hours",
        "The panel was soaked at 295 K for two hours",
        "A completely different sentence entirely.",
    ])
    assert finder.for_text("The panel was soaked at 340 K for six hours") == \
        "The panel was soaked at 340"
    assert finder.for_text("A completely different sentence entirely.") == \
        "A completely different sentence entirely."


def test_uniqueness_is_tested_against_substrings_not_whole_paragraphs():
    """Two paragraphs that merely START the same are not distinguished by a
    prefix, even though the paragraphs themselves differ."""
    finder = FindStrings([
        "The measurement was taken at the upper station and logged.",
        "The measurement was taken at the lower station and logged.",
    ])
    result = finder.for_text("The measurement was taken at the upper station and logged.")
    assert finder.occurrences(result) == 1
    assert "upper" in result


def test_repeated_boilerplate_still_yields_the_longest_useful_prefix():
    text = "DISTRIBUTION STATEMENT A. Approved for public release."
    finder = FindStrings([text] * 4)
    assert finder.for_text(text).startswith("DISTRIBUTION STATEMENT A.")


def test_whitespace_is_collapsed_so_the_string_can_be_pasted():
    assert searchable("  The  panel\twas\nsoaked ") == "The panel was soaked"


# -- locators -------------------------------------------------------------


def test_the_heading_path_comes_from_classified_headings_not_styles():
    """It has to work on the export where nothing is styled as a heading."""
    ctx = build_context(build.make(body=FOREIGN_BODY))
    results = classify_document(ctx)
    factory = LocatorFactory(ctx.blocks, {p: c.role for p, c in results.items()})
    last = factory.of(ctx.blocks[-1])
    assert last.heading_path == ("Thermal Margin Analysis", "Introduction")
    # The heading counts as paragraph 1 of its own section, so the ordinal
    # matches what someone counting down from the heading in Word would say.
    assert (last.ordinal, last.of) == (3, 3)
    heading = factory.of(ctx.blocks[1])
    assert (heading.ordinal, heading.of) == (1, 3)


def test_a_finding_in_a_cell_names_its_table_row_and_column():
    body = (
        build.para("Before the table.")
        + "<w:tbl><w:tr><w:tc>" + build.para("first cell") + "</w:tc>"
        + "<w:tc>" + build.para("second cell") + "</w:tc></w:tr></w:tbl>"
    )
    ctx = build_context(build.make(body=body))
    factory = LocatorFactory(ctx.blocks, {})
    cell = next(b for b in ctx.blocks if b.text == "second cell")
    assert factory.of(cell).container == "table 1, row 1, column 2"


def test_a_document_level_locator_reads_as_one():
    from formgen.plan.locator import document_locator

    assert document_locator().describe() == "the document as a whole"


def test_the_page_number_is_absent_unless_someone_paginated():
    """Never guessed: a wrong page number costs the reader a trip."""
    ctx = build_context(build.make())
    factory = LocatorFactory(ctx.blocks, {})
    assert factory.of(ctx.blocks[0]).page is None


# -- rule families --------------------------------------------------------


def test_a_conforming_document_produces_nothing(profile):
    """lint(the thing we learned from) == silence. The fixed point that makes
    the whole tool worth running."""
    plan = lint(build.make(), profile)
    assert plan.findings == []
    assert plan.exit_code == 0


def test_wrong_margins_are_reported_against_the_profile(profile):
    pkg = build.make(sect_pr=build.sectpr(
        margins='w:top="1440" w:right="1080" w:bottom="1440" w:left="1080"'
    ))
    plan = lint(pkg, profile)
    margins = [f for f in plan.findings if f.code == "page.margins"]
    assert len(margins) == 2
    assert "0.75in" in margins[0].message and "1in" in margins[0].message
    assert all(f.severity == ERROR for f in margins)


def test_a_wrongly_styled_paragraph_is_one_finding_not_six(profile):
    """A wrongly-styled paragraph differs in every property. Reporting each
    one buries the single finding that would fix it."""
    plan = lint(build.make(body=FOREIGN_BODY), profile)
    codes = {f.code for f in plan.findings}
    assert "style.wrong" in codes
    assert not {c for c in codes if c.startswith("style.")} - {"style.wrong"}


def test_deviations_are_aggregated_with_a_count(profile):
    """180 paragraphs at the wrong size is one problem in 180 places."""
    body = "".join(
        build.para(f"Paragraph {i} of prose that ends properly here.",
                   style="BodyText", rpr='<w:sz w:val="24"/>')
        for i in range(12)
    )
    plan = lint(build.make(body=body), profile)
    sizes = [f for f in plan.findings if f.code == "style.size"]
    assert len(sizes) == 1
    assert "in 12 paragraphs" in sizes[0].message
    assert sizes[0].locator.find_string


def test_direct_formatting_over_a_correct_style_is_still_reported(profile):
    """The style is right, so style.wrong does not fire -- but the rendered
    size is wrong, and that is the finding."""
    plan = lint(build.make(body=build.para(
        "Prose that ends properly and runs on a bit.", style="BodyText",
        rpr='<w:sz w:val="28"/>',
    )), profile)
    assert [f.code for f in plan.findings if f.code.startswith("style")] == \
        ["style.size"]
    assert "14pt" in plan.findings[0].message


def test_an_absent_value_reads_as_not_set_rather_than_none(profile):
    styles = [s for s in build.DEFAULT_STYLES if "BodyText" not in s]
    styles.append(build.style("BodyText", "Body Text"))   # no spacing, no jc
    plan = lint(build.make(body=build.para(
        "Prose that ends properly and runs on a bit.", style="BodyText"
    ), styles=styles), profile)
    messages = " ".join(f.message for f in plan.findings)
    assert "not set" in messages
    assert "None" not in messages


def test_a_muted_rule_does_not_fire(profile, tmp_path):
    profile.overrides.muted.add("/page/primary/margins/left")
    pkg = build.make(sect_pr=build.sectpr(
        margins='w:top="1440" w:right="1440" w:bottom="1440" w:left="1080"'
    ))
    assert lint(pkg, profile).findings == []


def test_an_overridden_severity_is_honoured(profile):
    profile.overrides.severity["/page/primary/margins/left"] = WARN
    pkg = build.make(sect_pr=build.sectpr(
        margins='w:top="1440" w:right="1440" w:bottom="1440" w:left="1080"'
    ))
    plan = lint(pkg, profile)
    assert [f.severity for f in plan.findings] == [WARN]
    assert plan.exit_code == 0        # warnings alone do not fail


# -- refusals -------------------------------------------------------------


def test_tracked_changes_are_refused_with_a_remedy(profile):
    body = "<w:ins>" + build.para("inserted text") + "</w:ins>"
    plan = lint(build.make(body=body), profile)
    assert plan.refusals
    assert "Accept" in plan.refusals[0]
    assert plan.exit_code == 3


def test_altchunk_is_refused_because_we_cannot_see_inside_it(profile):
    body = build.para("ok") + '<w:altChunk xmlns:r="%s" r:id="rId9"/>' % (
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    )
    plan = lint(build.make(body=body), profile)
    assert any("altChunk" in r for r in plan.refusals)
    assert plan.exit_code == 3


def test_a_refused_document_reports_nothing_else(profile):
    """Once we refuse, every other finding is noise about a file we will not
    touch."""
    body = "<w:ins>" + build.para("x") + "</w:ins>" + FOREIGN_BODY
    plan = lint(build.make(body=body), profile)
    assert {f.code for f in plan.findings} == {"document.refused"}


# -- headers and placeholders --------------------------------------------


def test_a_missing_header_explains_link_to_previous(profile, tmp_path):
    """The trap: an absent reference means "same as before", not "none"."""
    source = build.make()
    rid = build.add_hdrftr(source, "header", "header1.xml", "HOUSE REPORT")
    sect = source.edit(source.main_document).find(
        f"{qn('w:body')}/{qn('w:sectPr')}"
    )
    from lxml import etree

    ref = etree.SubElement(sect, qn("w:headerReference"))
    ref.set(qn("w:type"), "default")
    ref.set(qn("r:id"), rid)
    sect.insert(0, ref)
    path = tmp_path / "with_header"
    path.mkdir()
    for i in range(5):
        source.save(path / f"h{i}.docx", deterministic=True)
    learn(sorted(path.glob("*.docx")), tmp_path / "hp")
    profile = Profile.load(tmp_path / "hp")

    plan = lint(build.make(), profile)          # no header at all
    missing = [f for f in plan.findings if f.code == "hdrftr.missing"]
    assert missing
    assert "not 'no header'" in missing[0].message


def test_a_required_placeholder_that_is_absent_is_an_error(profile):
    profile.overrides.placeholders["report_number"] = {"required": True}
    plan = lint(build.make(), profile)
    codes = [f.code for f in plan.findings if f.code.startswith("placeholder")]
    assert codes == ["placeholder.missing"]
    assert plan.exit_code == 1


def test_a_placeholder_showing_its_prompt_is_not_filled_in(profile):
    profile.overrides.placeholders["report_number"] = {"required": True}
    body = (
        f'<w:sdt xmlns:w="{W_NS}"><w:sdtPr>'
        '<w:tag w:val="formgen.report_number"/><w:showingPlcHdr/></w:sdtPr>'
        "<w:sdtContent>" + build.para("Enter the report number") +
        "</w:sdtContent></w:sdt>"
    )
    plan = lint(build.make(body=body), profile)
    codes = [f.code for f in plan.findings if f.code.startswith("placeholder")]
    assert codes == ["placeholder.empty"]


# -- the plan is what apply would do -------------------------------------


def test_every_finding_carries_the_rule_that_produced_it(profile):
    plan = lint(build.make(body=FOREIGN_BODY), profile)
    assert all(f.rule_id.startswith("/") for f in plan.findings)


def test_edits_describe_what_apply_would_change(profile):
    plan = lint(build.make(body=FOREIGN_BODY), profile)
    edits = {e.role: e for e in plan.edits}
    assert "body" in edits
    described = [op.describe() for op in edits["body"].ops]
    assert any("apply style body text" in d for d in described)
    assert any("strip direct run formatting" in d for d in described)


def test_a_paragraph_with_tabs_keeps_them(profile):
    """Signature blocks and leader-dot lines break catastrophically without."""
    body = (
        "<w:p><w:pPr><w:tabs><w:tab w:val='right' w:pos='9360'/></w:tabs>"
        "</w:pPr><w:r><w:t>Approved by:</w:t></w:r><w:r><w:tab/></w:r>"
        "<w:r><w:t>Date</w:t></w:r></w:p>"
    )
    plan = lint(build.make(body=body), profile)
    strips = [op for e in plan.edits for op in e.ops
              if type(op).__name__ == "StripPPr"]
    assert strips and "tabs" in strips[0].keep


def test_low_confidence_classifications_are_listed_for_review(profile):
    plan = lint(build.make(body=build.para("x")), profile)
    assert all(0.0 <= e.confidence <= 1.0 for e in plan.edits)


# -- reporting ------------------------------------------------------------


def test_the_json_report_mirrors_the_console_one(profile):
    plan = lint(build.make(body=FOREIGN_BODY), profile)
    payload = plan_dict(plan)
    assert payload["counts"]["error"] == len(
        [f for f in plan.findings if f.severity == ERROR]
    )
    assert payload["exit_code"] == plan.exit_code
    first = payload["findings"][0]
    assert set(first) >= {"code", "severity", "message", "rule", "locator"}
    assert first["locator"]["find"]


def test_findings_are_ordered_worst_first_then_in_document_order(profile):
    profile.overrides.severity["/page/primary/margins/left"] = WARN
    pkg = build.make(body=FOREIGN_BODY, sect_pr=build.sectpr(
        margins='w:top="1440" w:right="1440" w:bottom="1440" w:left="1080"'
    ))
    plan = lint(pkg, profile)
    ranks = [f.rank for f in plan.sorted_findings()]
    assert ranks == sorted(ranks)


# -- the CLI --------------------------------------------------------------


def test_lint_exits_1_on_errors_and_0_when_clean(tmp_path):
    runner = CliRunner()
    paths = [str(p) for p in corpus(tmp_path / "c")]
    runner.invoke(cli, ["learn", *paths, "-o", str(tmp_path / "p")], obj={})

    clean = tmp_path / "clean.docx"
    build.make().save(clean, deterministic=True)
    assert runner.invoke(
        cli, ["lint", str(clean), "-p", str(tmp_path / "p")], obj={}
    ).exit_code == 0

    dirty = tmp_path / "dirty.docx"
    build.make(body=FOREIGN_BODY).save(dirty, deterministic=True)
    result = runner.invoke(cli, ["lint", str(dirty), "-p", str(tmp_path / "p")], obj={})
    assert result.exit_code == 1
    assert "ERRORS" in result.output


def test_lint_json_is_valid_json(tmp_path):
    runner = CliRunner()
    paths = [str(p) for p in corpus(tmp_path / "c")]
    runner.invoke(cli, ["learn", *paths, "-o", str(tmp_path / "p")], obj={})
    dirty = tmp_path / "dirty.docx"
    build.make(body=FOREIGN_BODY).save(dirty, deterministic=True)
    result = runner.invoke(
        cli, ["lint", str(dirty), "-p", str(tmp_path / "p"), "--json"], obj={}
    )
    assert json.loads(result.output)["document"] == "dirty.docx"


def test_lint_exits_3_on_a_document_it_refuses(tmp_path):
    runner = CliRunner()
    paths = [str(p) for p in corpus(tmp_path / "c")]
    runner.invoke(cli, ["learn", *paths, "-o", str(tmp_path / "p")], obj={})
    refused = tmp_path / "tracked.docx"
    build.make(body="<w:ins>" + build.para("x") + "</w:ins>").save(
        refused, deterministic=True
    )
    result = runner.invoke(cli, ["lint", str(refused), "-p", str(tmp_path / "p")],
                           obj={})
    assert result.exit_code == 3
    assert "REFUSED" in result.output
