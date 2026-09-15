"""Phase 5 end to end: a corpus in, a structural profile and a corrected
template out, and lint enforcing what came of it."""

from __future__ import annotations

import json

import pytest
from lxml import etree

from fixtures import build
from formgen.classify.features import build_context
from formgen.learn.materialize import TAG_PREFIX, wrap
from formgen.learn.pipeline import learn
from formgen.learn.placeholders import classify
from formgen.learn.skeleton import heading_key, properties_for, skeleton_for
from formgen.opc.ns import NS, qn
from formgen.opc.package import OpcPackage
from formgen.plan.builder import build_plan
from formgen.profile.io import Profile
from formgen.profile.sync import placeholders_in

STATEMENT = "Distribution Statement A: approved for public release."
INTRO = "The X-7 radiator exceeds its design margin under worst-case loading."
METHOD = "The panel was soaked at 340 K for six hours before measurement."


def exemplar(number, author, statement=STATEMENT, sections=("Introduction", "Methods")):
    body = (
        build.para("Thermal Margin Analysis", style="Title")
        + build.para(statement, style="BodyText")
        + build.para(f"Report No. {number}", style="BodyText")
        + build.para(f"Prepared by {author}", style="BodyText")
    )
    for heading in sections:
        body += build.para(heading, style="Heading1")
        body += build.para(INTRO if heading == "Introduction" else METHOD,
                           style="BodyText")
    return build.make(body, creator=author)


CORPUS = {
    "a": ("LR-2024-0041", "L. Craig"),
    "b": ("LR-2025-0118", "R. Patel"),
    "c": ("LR-2026-0142", "K. Ito"),
}


@pytest.fixture
def corpus(tmp_path):
    directory = tmp_path / "corpus"
    directory.mkdir()
    paths = []
    for name, (number, author) in CORPUS.items():
        path = directory / f"{name}.docx"
        exemplar(number, author).save(path, deterministic=True)
        paths.append(path)
    return paths


@pytest.fixture
def learned(tmp_path, corpus):
    result = learn(corpus, tmp_path / "prof", name="lab-report",
                   generated="2026-09-15T00:00:00")
    return result, Profile.load(tmp_path / "prof")


# -- the bridge into the aligner -----------------------------------------


def test_a_typed_heading_number_does_not_make_a_different_section():
    assert heading_key("2.3 Thermal Soak") == heading_key("Thermal Soak")
    assert heading_key("IV. Results") == heading_key("Results")
    assert heading_key("1") == "1"          # nothing left if we stripped it


def test_document_properties_are_flattened_for_naming():
    properties = properties_for(build.make(creator="L. Craig", title="A Report"))
    assert properties["creator"] == "L. Craig"
    assert properties["title"] == "A Report"


def test_the_skeleton_finds_the_sections_the_corpus_shares(corpus):
    packages = {p.stem: OpcPackage.open(p) for p in corpus}
    skeleton = skeleton_for(packages)
    assert [s.title for s in skeleton.sections] == ["", "Introduction", "Methods"]


def test_table_cells_align_by_their_coordinates(tmp_path):
    """A cover page is usually a borderless layout table, and that is where
    the fields are: a label cell beside a value cell."""
    def cover(number):
        cells = [("Report Number:", number), ("Status:", "Draft")]
        rows = "".join(
            "<w:tr>" + "".join(
                f"<w:tc><w:tcPr/>{build.para(text, style='BodyText')}</w:tc>"
                for text in pair
            ) + "</w:tr>"
            for pair in cells
        )
        return build.make(
            f'<w:tbl><w:tblPr/><w:tblGrid/>{rows}</w:tbl>'
            + build.para("Introduction", style="Heading1")
            + build.para(INTRO, style="BodyText")
        )

    packages = {doc: cover(f"LR-202{i}-0001") for i, doc in enumerate("abc")}
    profile = classify(skeleton_for(packages))
    labels = [s.text for s in profile.slots if s.kind == "boilerplate"]
    assert "Report Number:" in labels
    assert any(s.name == "report_number" for s in profile.placeholders)


# -- materializing into the donor ----------------------------------------


def paragraph(text, rpr=""):
    return etree.fromstring(
        f'<w:p xmlns:w="{NS["w"]}"><w:r>{rpr}<w:t>{text}</w:t></w:r></w:p>'
    )


def test_wrapping_part_of_a_paragraph_splits_the_run_and_keeps_its_formatting():
    block = paragraph("Report No. LR-2024-0041", "<w:rPr><w:b/></w:rPr>")
    assert wrap(block, "report_number", "LR-2024-0041") is None
    sdt = block.find(qn("w:sdt"))
    assert sdt is not None
    tag = sdt.find(f"{qn('w:sdtPr')}/{qn('w:tag')}")
    assert tag.get(qn("w:val")) == TAG_PREFIX + "report_number"
    inner = sdt.find(f"{qn('w:sdtContent')}/{qn('w:r')}")
    assert inner.find(f"{qn('w:t')}").text == "LR-2024-0041"
    assert inner.find(f"{qn('w:rPr')}/{qn('w:b')}") is not None
    assert "".join(block.itertext()) == "Report No. LR-2024-0041"


def test_a_whole_paragraph_placeholder_needs_no_split():
    block = paragraph("L. Craig")
    assert wrap(block, "author", "L. Craig") is None
    assert len(block.findall(qn("w:r"))) == 0
    assert "".join(block.itertext()) == "L. Craig"


def test_an_ambiguous_value_is_left_alone_rather_than_guessed_at():
    block = paragraph("Draft - Draft")
    assert wrap(block, "status", "Draft") == \
        "the value appears more than once in the paragraph"
    assert block.find(qn("w:sdt")) is None


def test_a_value_that_is_not_in_the_paragraph_is_refused():
    assert wrap(paragraph("Report No. LR-1"), "x", "LR-9") is not None


def test_the_control_id_is_stable_across_runs():
    one, two = paragraph("L. Craig"), paragraph("L. Craig")
    wrap(one, "author", "L. Craig")
    wrap(two, "author", "L. Craig")
    assert etree.tostring(one) == etree.tostring(two)


def test_a_materialized_control_is_not_locked():
    """A lock makes the user's in-Word edits bounce, which would break the
    correction workflow the controls exist for."""
    block = paragraph("L. Craig")
    wrap(block, "author", "L. Craig")
    assert block.find(f".//{qn('w:lock')}") is None
    assert block.find(f".//{qn('w:dataBinding')}") is None


# -- learn --------------------------------------------------------------


def test_learn_writes_a_skeleton_into_the_profile(learned):
    _, profile = learned
    assert [s["section"] for s in profile.required_sections()] == \
        ["Introduction", "Methods"]
    assert any(s["kind"] == "placeholder" for s in profile.slots())


def test_the_report_number_is_found_typed_and_patterned(learned):
    result, _ = learned
    slot = next(s for s in result.skeleton.placeholders
                if s.split and s.split.prefix.startswith("Report No"))
    assert slot.value_type == "identifier"
    assert slot.pattern == r"^[A-Z]{2}\-\d{4}\-\d{4}$"
    assert set(slot.examples) == {n for n, _ in CORPUS.values()}


def test_the_statement_every_exemplar_shares_becomes_boilerplate(learned):
    result, _ = learned
    assert any(s.text == STATEMENT for s in result.skeleton.boilerplate)


def test_placeholders_become_content_controls_in_the_template(learned):
    result, profile = learned
    assert result.materialized.wrapped
    controls = placeholders_in(OpcPackage.open(profile.template))
    assert set(result.materialized.wrapped) <= set(controls)


def test_the_template_is_still_structurally_sound_after_materializing(learned):
    _, profile = learned
    package = OpcPackage.open(profile.template)
    assert package.dangling_rels() == []
    context = build_context(package)
    assert STATEMENT in "\n".join(f.text for f in context.features)


def test_the_inferred_type_reaches_overrides_where_a_user_can_change_it(learned):
    _, profile = learned
    entry = next(v for k, v in profile.placeholders.items()
                 if v.get("type") == "identifier")
    assert entry["pattern"].startswith("^")
    assert entry["required"] is True
    assert entry["tag"].startswith(TAG_PREFIX)


def test_a_guessed_name_fails_the_build_but_still_writes_it(learned, tmp_path):
    result, _ = learned
    assert result.unconfident
    assert (tmp_path / "prof" / "profile.json").exists()
    assert any("confirm it in template.docx" in n for n in result.notes)


def test_two_exemplars_are_not_enough_to_align(tmp_path, corpus):
    result = learn(corpus[:2], tmp_path / "small", generated="x")
    assert result.skeleton is None
    document = json.loads((tmp_path / "small" / "profile.json").read_text())
    assert document["skeleton"] is None


def test_learning_twice_gives_the_same_template(tmp_path, corpus):
    one = learn(corpus, tmp_path / "p1", generated="x").template_sha
    two = learn(corpus, tmp_path / "p2", generated="x").template_sha
    assert one == two


# -- lint ----------------------------------------------------------------


def lint(package, profile):
    return build_plan(package, profile, document_name="doc.docx")


def test_a_conforming_document_raises_no_structural_finding(learned):
    _, profile = learned
    plan = lint(exemplar("LR-2027-0001", "M. Rao"), profile)
    assert [f for f in plan.findings if f.code.startswith("structure.")] == []


def test_a_missing_section_is_reported(learned):
    _, profile = learned
    plan = lint(exemplar("LR-2027-0001", "M. Rao", sections=("Introduction",)),
                profile)
    finding = next(f for f in plan.findings
                   if f.code == "structure.section_missing")
    assert "Methods" in finding.message


def test_sections_in_the_wrong_order_are_reported(learned):
    _, profile = learned
    plan = lint(exemplar("LR-2027-0001", "M. Rao",
                         sections=("Methods", "Introduction")), profile)
    finding = next(f for f in plan.findings
                   if f.code == "structure.section_order")
    assert "introduction > methods" in finding.message


def test_altered_boilerplate_is_an_error_that_points_at_the_paragraph(learned):
    _, profile = learned
    plan = lint(exemplar("LR-2027-0001", "M. Rao",
                         statement="Distribution Statement A: approved for "
                                   "public relase."), profile)
    finding = next(f for f in plan.findings
                   if f.code == "structure.boilerplate_altered")
    assert finding.severity == "error"
    assert finding.expected == STATEMENT
    assert finding.locator.find_string


def test_absent_boilerplate_is_a_warning_because_nothing_can_add_it(learned):
    _, profile = learned
    plan = lint(exemplar("LR-2027-0001", "M. Rao", statement="Unrelated text."),
                profile)
    finding = next(f for f in plan.findings
                   if f.code == "structure.boilerplate_missing")
    assert finding.severity == "warn"


def test_prose_under_a_heading_is_never_enforced_as_boilerplate(learned):
    """Three exemplars will agree on plenty of ordinary sentences. Enforcing
    those would tell every author their Introduction is wrong."""
    _, profile = learned
    body = (
        build.para("Thermal Margin Analysis", style="Title")
        + build.para(STATEMENT, style="BodyText")
        + build.para("Report No. LR-2027-0001", style="BodyText")
        + build.para("Prepared by M. Rao", style="BodyText")
        + build.para("Introduction", style="Heading1")
        + build.para("Something else entirely.", style="BodyText")
        + build.para("Methods", style="Heading1")
        + build.para("And another thing.", style="BodyText")
    )
    plan = lint(build.make(body), profile)
    assert [f for f in plan.findings if f.code.startswith("structure.")] == []


def test_a_field_value_of_the_wrong_shape_warns_and_names_the_shape(learned):
    _, profile = learned
    plan = lint(exemplar("12", "M. Rao"), profile)
    finding = next(f for f in plan.findings if f.code == "placeholder.pattern")
    assert finding.severity == "warn"
    assert "'12'" in finding.message


def test_a_field_found_by_its_wording_is_not_reported_missing(learned):
    """A foreign document has no content controls. Finding the field by the
    literal wording around it is what makes the rule usable at all."""
    _, profile = learned
    plan = lint(exemplar("LR-2027-0001", "M. Rao"), profile)
    assert [f for f in plan.findings if f.code == "placeholder.missing"] == []


def test_a_field_with_no_wording_to_find_it_by_is_declared_unverifiable(learned):
    _, profile = learned
    profile.overrides.placeholders["reviewer"] = {"required": True,
                                                  "type": "person"}
    plan = lint(exemplar("LR-2027-0001", "M. Rao"), profile)
    # Declared by hand, so it is a requirement the user stated, not a guess.
    assert any(f.code == "placeholder.missing" for f in plan.findings)


# -- the correction loop -------------------------------------------------


def test_a_name_corrected_in_word_survives_relearning(tmp_path, corpus):
    """The failure this exists to prevent: a user fixes a field's name, the
    format is learned again with more exemplars, and their fix vanishes."""
    from formgen.profile.sync import sync

    directory = tmp_path / "prof"
    first = learn(corpus, directory, generated="x")
    guessed = first.skeleton.placeholders[0]
    assert guessed.name_confidence < 1.0

    package = OpcPackage.open(directory / "template.docx")
    for tag in package.element(package.main_document).iter(qn("w:tag")):
        if tag.get(qn("w:val")) == TAG_PREFIX + guessed.name:
            tag.set(qn("w:val"), TAG_PREFIX + "report_number")
    package.touch(package.main_document)
    package.save(directory / "template.docx", deterministic=True)
    sync(directory, today="2026-09-15")

    bigger = list(corpus)
    for i, (number, author) in enumerate(
            [("LR-2027-0007", "M. Rao"), ("LR-2028-0031", "S. Bell")]):
        path = corpus[0].parent / f"extra{i}.docx"
        exemplar(number, author).save(path, deterministic=True)
        bigger.append(path)

    again = learn(bigger, directory, generated="x")
    names = [s.name for s in again.skeleton.placeholders]
    assert "report_number" in names
    assert guessed.name not in names
    assert again.unconfident == []


def test_the_frame_is_what_joins_a_field_across_a_relearn(tmp_path, corpus):
    """The exemplars have no content controls, so the tag cannot be the join
    key. The wording around the value is the same in both, and can be."""
    from formgen.profile.sync import placeholders_in

    learn(corpus, tmp_path / "prof", generated="x")
    controls = placeholders_in(OpcPackage.open(tmp_path / "prof" / "template.docx"))
    frames = [entry.get("template") for entry in controls.values()]
    assert any(f and f.startswith("Report No. {") for f in frames)
