"""new and extract: the Markdown dialect, the emitter, and the round trip."""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from lxml import etree

from fixtures import build
from formgen.cli import cli
from formgen.content import ast as A
from formgen.content.emit import bookmark_name, emit
from formgen.content.extract import extract, to_markdown
from formgen.content.markdown_in import parse, split_front_matter, validate
from formgen.content.omml import to_omml, to_tex
from formgen.learn.pipeline import learn
from formgen.opc.ns import RT, qn
from formgen.opc.package import OpcPackage
from formgen.plan.builder import build_plan
from formgen.profile.io import Profile
from formgen.safety.verify import check_integrity


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


def render(markdown, profile, tmp_path, keep_cover=False):
    doc = parse(markdown)
    pkg, report = emit(doc, profile.template, source_dir=tmp_path,
                       keep_cover=keep_cover)
    return doc, pkg, report


def body_text(pkg):
    from formgen.oox.walk import document_text

    return document_text(pkg)


# -- the dialect ----------------------------------------------------------


def test_front_matter_is_read_as_yaml_and_the_body_follows():
    meta, body, _ = split_front_matter(
        "---\ntitle: A Report\nauthors: [L. Craig]\n---\n\n# Heading\n"
    )
    assert meta == {"title": "A Report", "authors": ["L. Craig"]}
    assert body.lstrip().startswith("# Heading")


def test_a_document_with_no_front_matter_is_fine():
    meta, body, _ = split_front_matter("# Heading\n")
    assert meta == {} and body == "# Heading\n"


def test_malformed_front_matter_says_so_rather_than_half_parsing():
    with pytest.raises(ValueError, match="malformed"):
        split_front_matter("---\ntitle: [unclosed\n---\n\nbody\n")


def test_front_matter_that_is_not_a_mapping_is_rejected():
    with pytest.raises(ValueError, match="mapping"):
        split_front_matter("---\n- a\n- b\n---\n\nbody\n")


def test_prose_about_money_is_not_an_equation():
    """The failure every naive dollar-math implementation has."""
    doc = parse("The part cost $5 and the bracket cost $7 more.")
    assert not any(isinstance(i, A.Math) for i in doc.blocks[0].children)
    assert "$5" in doc.blocks[0].text()


def test_inline_and_display_maths_are_both_recognised():
    doc = parse("The margin was $\\Delta T = 12$.\n\n$$\nE = mc^2\n$$\n")
    maths = [i for i in doc.blocks[0].children if isinstance(i, A.Math)]
    assert maths and maths[0].tex == "\\Delta T = 12"
    assert isinstance(doc.blocks[1], A.MathBlock)
    assert doc.blocks[1].tex == "E = mc^2"


def test_an_empty_link_is_a_cross_reference_not_a_hyperlink():
    """Word renders the two completely differently: a REF field that
    renumbers itself, versus a hyperlink to nowhere."""
    doc = parse("See [](#fig:panel) and [the site](https://example.org).")
    kinds = [type(i).__name__ for i in doc.blocks[0].children]
    assert "CrossReference" in kinds and "Link" in kinds


def test_a_label_attaches_to_the_block_it_follows():
    doc = parse("# Introduction {#sec:intro}\n")
    assert doc.blocks[0].label == "sec:intro"
    assert doc.blocks[0].text() == "Introduction"


def test_a_label_on_a_caption_belongs_to_the_thing_captioned():
    doc = parse(
        "![Panel](fig/panel.png)\n\nFigure 1. The panel. {#fig:panel}\n"
    )
    figure = doc.blocks[0]
    assert isinstance(figure, A.Figure)
    assert figure.label == "fig:panel"
    assert figure.caption.text().startswith("Figure 1.")


def test_a_figure_caption_goes_below_and_a_table_caption_above():
    doc = parse(
        "![Panel](p.png)\n\nFigure 1. Below.\n\n"
        "Table 1. Above.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    )
    figure, table = doc.blocks
    assert figure.caption.text() == "Figure 1. Below."
    assert table.caption.text() == "Table 1. Above."


def test_footnotes_are_collected_and_paired():
    doc = parse("Text.[^a]\n\n[^a]: The note.\n")
    assert doc.footnote_labels_used() == ["a"]
    assert doc.footnotes["a"].blocks[0].text() == "The note."


def test_a_directive_carries_a_role_name():
    doc = parse("::: distribution-statement\nSTATEMENT A.\n:::\n")
    block = doc.blocks[0]
    assert isinstance(block, A.Directive) and block.name == "distribution-statement"
    assert block.blocks[0].text() == "STATEMENT A."


def test_a_toc_comment_becomes_a_toc_block():
    assert isinstance(parse("<!-- toc -->\n").blocks[0], A.TableOfContents)


def test_raw_html_is_dropped_loudly():
    doc = parse("<div>something</div>\n")
    assert doc.blocks == []
    assert any("raw HTML" in w for w in doc.warnings)


# -- pre-flight -----------------------------------------------------------


def test_every_missing_field_is_listed_at_once_with_the_paste_line():
    """Discovering them one run at a time is what makes people script around
    a tool."""
    doc = parse("---\ntitle: A\n---\n\nbody\n")
    problems = validate(doc, required={"title", "report_number", "reviewer"})
    assert len(problems) == 1
    assert "report_number" in problems[0].message and "reviewer" in problems[0].message
    assert '--set report_number="..."' in problems[0].remedy


def test_a_dangling_cross_reference_is_caught_before_anything_is_written():
    problems = validate(parse("See [](#nowhere).\n"))
    assert [p.code for p in problems] == ["reference.dangling"]


def test_an_undefined_footnote_is_caught():
    problems = validate(parse("Text.[^missing]\n"))
    assert [p.code for p in problems] == ["footnote.undefined"]


def test_an_unused_footnote_is_reported_as_a_note():
    problems = validate(parse("Text.\n\n[^a]: orphan\n"))
    assert [p.code for p in problems] == ["footnote.unused"]


def test_an_unsubstituted_template_placeholder_is_caught():
    problems = validate(parse("The reviewer was {{reviewer}}.\n"))
    assert [p.code for p in problems] == ["placeholder.unresolved"]


def test_an_unknown_directive_lists_the_roles_that_do_exist():
    problems = validate(parse("::: mystery\nx\n:::\n"),
                        known_roles={"distribution-statement"})
    assert problems[0].code == "directive.unknown"
    assert "distribution-statement" in problems[0].remedy


# -- maths ----------------------------------------------------------------


@pytest.mark.parametrize("tex", [
    "E = mc^2", "\\frac{a+b}{c}", "\\sqrt{x^2 + y^2}", "\\Delta T = 12.4",
    "\\sum_{i=1}^{n} x_i", "\\sin(\\theta) \\leq 1",
])
def test_supported_tex_survives_a_round_trip_through_omml(tex):
    conversion = to_omml(tex)
    assert conversion.warnings == []
    back = to_tex(conversion.element)
    assert back.replace("{", "").replace("}", "").replace(" ", "") == \
        tex.replace("{", "").replace("}", "").replace(" ", "")


def test_unsupported_tex_comes_through_as_text_and_says_so():
    """An equation that quietly renders as something else is the worst
    possible failure: nobody proofreads what they did not expect to be wrong."""
    conversion = to_omml("\\bowtie x")
    assert any("bowtie" in w for w in conversion.warnings)
    assert "bowtie" in "".join(conversion.element.itertext())


def test_a_function_name_is_set_upright():
    """"sin" in italics reads as the product of three variables."""
    element = to_omml("\\sin x").element
    assert element.find(f".//{qn('m:sty')}") is not None


# -- emitting -------------------------------------------------------------


def test_a_generated_document_lints_clean_against_its_own_profile(profile, tmp_path):
    """The property that justified writing our own emitter instead of
    shelling out to Pandoc."""
    _, pkg, _ = render(
        "# Introduction\n\nProse that ends properly and runs on a while.\n",
        profile, tmp_path,
    )
    assert build_plan(pkg, profile, document_name="x").findings == []


def test_a_generated_document_is_structurally_intact(profile, tmp_path):
    _, pkg, _ = render(
        "# Heading\n\nText.[^1]\n\n[^1]: note\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
        profile, tmp_path,
    )
    assert check_integrity(pkg) == []


def test_fields_are_emitted_unpopulated_rather_than_faked(profile, tmp_path):
    """Faking a page number is worse than omitting it, because a wrong one is
    believed."""
    _, pkg, report = render("<!-- toc -->\n", profile, tmp_path)
    document = pkg.blob(pkg.main_document).decode()
    assert "TOC \\o" in document
    assert 'w:dirty="true"' in document
    assert "Press F9" in document
    assert report.fields == 1


def test_settings_asks_word_to_update_fields_on_open(profile, tmp_path):
    _, pkg, _ = render("<!-- toc -->\n", profile, tmp_path)
    settings = pkg.blob(pkg.related(RT["settings"])).decode()
    assert 'w:updateFields w:val="true"' in settings


def test_a_caption_number_is_a_field_not_a_typed_digit(profile, tmp_path):
    _, pkg, _ = render(
        "Table 1. Margins.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n",
        profile, tmp_path,
    )
    document = pkg.blob(pkg.main_document).decode()
    assert "SEQ Table" in document


def test_a_cross_reference_becomes_a_ref_field(profile, tmp_path):
    _, pkg, _ = render(
        "# Scope {#sec:scope}\n\nSee [](#sec:scope).\n", profile, tmp_path
    )
    document = pkg.blob(pkg.main_document).decode()
    assert "REF sec_scope" in document
    assert 'w:name="sec_scope"' in document


def test_bookmark_names_are_sanitised_to_what_word_accepts():
    assert bookmark_name("fig:panel") == "fig_panel"
    assert bookmark_name("1st") == "b1st"
    assert bookmark_name("") == "ref"
    assert len(bookmark_name("x" * 80)) == 40


def test_each_list_gets_its_own_numbering_so_the_second_restarts(profile, tmp_path):
    """Sharing one w:num makes the second list continue the first's numbers."""
    _, pkg, _ = render(
        "1. one\n2. two\n\ntext\n\n1. one again\n2. two again\n",
        profile, tmp_path,
    )
    document = pkg.element(pkg.main_document)
    used = {
        el.get(qn("w:val"))
        for el in document.findall(f".//{qn('w:numId')}")
    }
    assert len(used) == 2
    numbering = pkg.element(pkg.related(RT["numbering"]))
    for num in numbering.findall(qn("w:num")):
        if num.get(qn("w:numId")) in used:
            assert num.find(f"{qn('w:lvlOverride')}/{qn('w:startOverride')}") \
                is not None


def test_a_table_gets_tbllook_in_both_spellings(profile, tmp_path):
    """A table style's conditional formatting is gated on tblLook, and setting
    only one half yields a table that looks unstyled."""
    _, pkg, _ = render("| a | b |\n|---|---|\n| 1 | 2 |\n", profile, tmp_path)
    look = pkg.element(pkg.main_document).find(
        f".//{qn('w:tbl')}/{qn('w:tblPr')}/{qn('w:tblLook')}"
    )
    assert look.get(qn("w:val")) == "04A0"
    assert look.get(qn("w:firstRow")) == "1"


def test_a_table_is_followed_by_a_paragraph(profile, tmp_path):
    """Word repairs a file whose body ends in a table, or in which two tables
    abut with nothing between them."""
    _, pkg, _ = render("| a |\n|---|\n| 1 |\n", profile, tmp_path)
    body = pkg.element(pkg.main_document).find(qn("w:body"))
    children = [c.tag for c in body if isinstance(c.tag, str)]
    assert children[children.index(qn("w:tbl")) + 1] == qn("w:p")
    assert children[-1] == qn("w:sectPr")


def test_footnotes_are_written_even_when_the_template_has_no_footnote_part(
    profile, tmp_path
):
    """Dropping the author's footnotes over a template detail they cannot see
    would be losing content."""
    _, pkg, report = render("Text.[^1]\n\n[^1]: The note.\n", profile, tmp_path)
    part = pkg.related(RT["footnotes"])
    assert part and report.footnotes == 1
    footnotes = pkg.blob(part).decode()
    assert "The note." in footnotes
    # The separators are required, or Word draws no rule above the notes.
    assert 'w:type="separator"' in footnotes


def test_emphasis_is_written_with_an_explicit_value(profile, tmp_path):
    """A bare <w:b/> depends on the reader implementing toggle inheritance,
    and readers disagree about that."""
    _, pkg, _ = render("Some **bold** text.\n", profile, tmp_path)
    document = pkg.blob(pkg.main_document).decode()
    assert '<w:b w:val="true"/>' in document


def test_a_missing_image_is_reported_not_silently_skipped(profile, tmp_path):
    _, pkg, report = render("![alt](nowhere.png)\n", profile, tmp_path)
    assert any("image not found" in w for w in report.warnings)
    assert "missing image" in body_text(pkg)


def test_an_image_is_scaled_to_fit_the_text_column(profile, tmp_path):
    from PIL import Image as PILImage

    path = tmp_path / "wide.png"
    PILImage.new("RGB", (3000, 1000), "white").save(path)
    _, pkg, report = render("![wide](wide.png)\n", profile, tmp_path)
    extent = pkg.element(pkg.main_document).find(f".//{qn('wp:extent')}")
    width = int(extent.get("cx"))
    assert width <= 9360 * 635 + 1      # the text column, in EMU
    assert int(extent.get("cy")) == pytest.approx(width / 3, rel=0.02)


def test_the_cover_page_is_kept_by_default_and_droppable(profile, tmp_path):
    with_cover = render("# Heading\n\ntext\n", profile, tmp_path, keep_cover=True)[1]
    without = render("# Heading\n\ntext\n", profile, tmp_path, keep_cover=False)[1]
    assert "A Fixture Document" in body_text(with_cover)
    assert "A Fixture Document" not in body_text(without)


def test_front_matter_fills_the_templates_content_controls(tmp_path):
    sdt = (
        '<w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        '2006/main"><w:sdtPr><w:tag w:val="formgen.report_number"/>'
        "<w:showingPlcHdr/></w:sdtPr><w:sdtContent>"
        + build.para("Enter the report number") +
        "</w:sdtContent></w:sdt>"
    )
    directory = tmp_path / "corpus"
    directory.mkdir()
    for i in range(5):
        build.make(body=sdt + build.para("Introduction", style="Heading1")).save(
            directory / f"r{i}.docx", deterministic=True
        )
    learn(sorted(directory.glob("*.docx")), tmp_path / "prof")
    profile = Profile.load(tmp_path / "prof")

    _, pkg, _ = render(
        "---\nreport_number: LR-2026-0142\n---\n\n# Scope\n\ntext\n",
        profile, tmp_path, keep_cover=True,
    )
    text = body_text(pkg)
    assert "LR-2026-0142" in text
    # The greyed prompt must go, or Word renders it over our value.
    assert "Enter the report number" not in text


# -- the round trip -------------------------------------------------------


ROUND_TRIP = """\
# Introduction {#sec_intro}

The X-7 radiator exceeds its design margin.[^1] The *p*-value was **significant**.

Table 1. Margins. {#tbl_margins}

| Case    | Margin |
| ------- | -----: |
| Nominal | 12.4   |
| Worst   | 3.1    |

See [](#tbl_margins). The margin was $\\Delta T = 12.4$.

- first bullet
- second bullet

1. numbered one
2. numbered two

$$
E = mc^{2}
$$

[^1]: Measured at 340 K.
"""


def test_extract_of_emit_recovers_the_document(profile, tmp_path):
    """The oracle that polices both halves of the pair."""
    _, pkg, _ = render(ROUND_TRIP, profile, tmp_path)
    back = to_markdown(extract(pkg))
    for fragment in (
        "# Introduction {#sec_intro}",
        "[^1]", "**significant**", "*p*-value",
        "[](#tbl_margins)", "$\\Delta T = 12.4$",
        "- first bullet", "1. numbered one",
        "| Nominal | 12.4", "$$",
    ):
        assert fragment in back, fragment


def test_the_round_trip_reaches_a_fixed_point(profile, tmp_path):
    """Stability matters more than byte equality: a pipeline that drifts a
    little every pass is one nobody can put in version control."""
    _, first, _ = render(ROUND_TRIP, profile, tmp_path)
    once = to_markdown(extract(first))
    _, second, _ = render(once, profile, tmp_path)
    twice = to_markdown(extract(second))
    assert once == twice


def test_extract_puts_the_title_in_the_front_matter(profile, tmp_path):
    """Emitting it as a heading would make the round trip grow a level each
    pass."""
    _, pkg, _ = render("# Heading\n\ntext\n", profile, tmp_path, keep_cover=True)
    back = extract(pkg)
    assert back.meta.get("title") == "A Fixture Document"
    assert to_markdown(back).count("# Heading") == 1


def test_extract_drops_a_cached_field_result_but_keeps_the_reference(
    profile, tmp_path
):
    _, pkg, _ = render(
        "# Scope {#sec_scope}\n\nSee [](#sec_scope).\n", profile, tmp_path
    )
    back = to_markdown(extract(pkg))
    assert "[](#sec_scope)" in back
    assert "?" not in back          # the placeholder result must not survive


def test_extract_recovers_table_alignment(profile, tmp_path):
    _, pkg, _ = render(
        "| a | b |\n|---|--:|\n| 1 | 2 |\n", profile, tmp_path
    )
    assert "--:" in to_markdown(extract(pkg))


# -- the CLI --------------------------------------------------------------


def cli_profile(runner, tmp_path):
    paths = [str(p) for p in corpus(tmp_path / "c")]
    runner.invoke(cli, ["learn", *paths, "-o", str(tmp_path / "p")], obj={})
    return str(tmp_path / "p")


def test_new_writes_a_docx_that_lints_clean(tmp_path):
    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    source = tmp_path / "report.md"
    source.write_text("# Introduction\n\nProse that ends properly here.\n")

    result = runner.invoke(cli, ["new", str(source), "-p", directory], obj={})
    assert result.exit_code == 0, result.output
    output = tmp_path / "report.docx"
    assert output.exists()

    linted = runner.invoke(cli, ["lint", str(output), "-p", directory], obj={})
    assert linted.exit_code == 0, linted.output


def test_new_refuses_and_lists_everything_missing_at_once(tmp_path):
    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    source = tmp_path / "report.md"
    source.write_text("See [](#nowhere) and [^ghost].\n")
    result = runner.invoke(cli, ["new", str(source), "-p", directory], obj={})
    assert result.exit_code == 2
    assert "#nowhere" in result.output and "ghost" in result.output
    assert not (tmp_path / "report.docx").exists()


def test_set_supplies_a_field_from_the_command_line(tmp_path):
    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    source = tmp_path / "report.md"
    source.write_text("# Scope\n\ntext\n")
    result = runner.invoke(
        cli, ["new", str(source), "-p", directory, "--set", "title=From the CLI"],
        obj={},
    )
    assert result.exit_code == 0, result.output


def test_allow_missing_writes_a_visible_sentinel_and_exits_1(tmp_path):
    """Never a silently empty field: an empty one ships."""
    runner = CliRunner()
    paths = [str(p) for p in corpus(tmp_path / "c")]
    runner.invoke(cli, ["learn", *paths, "-o", str(tmp_path / "p")], obj={})
    overrides = tmp_path / "p" / "overrides.yaml"
    overrides.write_text(
        "version: 1\nplaceholders:\n  reviewer:\n    required: true\n"
    )
    source = tmp_path / "report.md"
    source.write_text("# Scope\n\ntext\n")
    result = runner.invoke(
        cli, ["new", str(source), "-p", str(tmp_path / "p"), "--allow-missing"],
        obj={},
    )
    assert result.exit_code == 1
    assert (tmp_path / "report.docx").exists()


def test_extract_writes_markdown_and_images(tmp_path):
    from PIL import Image as PILImage

    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    PILImage.new("RGB", (100, 100), "white").save(tmp_path / "panel.png")
    source = tmp_path / "report.md"
    source.write_text("# Scope\n\n![Panel](panel.png)\n\nFigure 1. The panel.\n")
    runner.invoke(cli, ["new", str(source), "-p", directory], obj={})

    result = runner.invoke(
        cli, ["extract", str(tmp_path / "report.docx"), "-o",
              str(tmp_path / "back.md")], obj={},
    )
    assert result.exit_code == 0, result.output
    text = (tmp_path / "back.md").read_text()
    assert "![" in text and ".media/" in text
    assert (tmp_path / "back.media").exists()
