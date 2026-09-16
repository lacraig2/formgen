"""Learning a format from documents that have no styles.

48% of the real documents we tested against use two or fewer paragraph
styles: Google Docs export, PDF conversion and plain hand-formatting all
leave every paragraph as `Normal` with the format written out on the runs.
That is not an edge case, it is half the population, and a style-keyed
ballot learns nothing from it but the body size.

Each test here runs the same corpus twice -- once styled, once put through
the converter -- and asserts the two agree. The documents look identical in
Word, so anything the tool can read off one it should read off the other.
"""

from __future__ import annotations

import json

import pytest

from fixtures import build, convert
from formgen.classify.features import build_context
from formgen.learn.pipeline import learn
from formgen.learn.rolestyles import style_name_for
from formgen.opc.ns import RT, qn
from formgen.opc.package import OpcPackage
from formgen.oox.styles import IMPLICIT_SIZE, StyleGraph, normalize_style_name
from formgen.plan.builder import _role_to_style, build_plan
from formgen.plan.execute import execute
from formgen.profile.io import Profile
from formgen.safety.verify import check_integrity


def exemplar(index: int) -> str:
    return (
        build.para("Thermal Margin Analysis", style="Title")
        + build.para(f"Report No. LR-202{index}-0041", style="BodyText")
        + build.para("Introduction", style="Heading1")
        + build.para("The X-7 radiator exceeds its design margin under load.",
                     style="BodyText")
        + build.para("Methods", style="Heading1")
        + build.para("The panel was soaked at 340 K for six hours.",
                     style="BodyText")
    )


@pytest.fixture
def corpora(tmp_path):
    """The same six documents, styled and converted."""
    out = {}
    for label, flatten in (("styled", False), ("unstyled", True)):
        directory = tmp_path / label
        directory.mkdir()
        for index in range(6):
            pkg = build.make(exemplar(index))
            if flatten:
                pkg = convert.flatten(pkg)
            pkg.save(directory / f"r{index}.docx", deterministic=True)
        out[label] = sorted(directory.glob("*.docx"))
    return out


@pytest.fixture
def learned(tmp_path, corpora):
    return {
        label: learn(paths, tmp_path / f"p-{label}", generated="x")
        for label, paths in corpora.items()
    }


# -- the inheritance root ------------------------------------------------


def test_a_document_that_specifies_no_size_anywhere_still_has_one():
    """Word applies its own default when docDefaults omits w:sz. Without
    that anchor every ratio in the classifier has no denominator: on a real
    clinical protocol, five words that carried a size outvoted six hundred
    and eighty-six that did not, and the document read as 18pt throughout."""
    pkg = build.make()
    root = pkg.element(pkg.related(RT["styles"]))
    for size in list(root.iter(qn("w:sz"))):
        size.getparent().remove(size)
    graph = StyleGraph.parse(root)
    assert graph.default_run.size == IMPLICIT_SIZE


def test_an_explicit_default_is_not_overridden_by_the_implicit_one():
    pkg = build.make(default_sz=24)
    graph = StyleGraph.parse(pkg.element(pkg.related(RT["styles"])))
    assert graph.default_run.size.points == 12


# -- what the converter does ---------------------------------------------


def test_the_converted_corpus_really_has_no_styles(corpora):
    pkg = OpcPackage.open(corpora["unstyled"][0])
    graph = StyleGraph.parse(pkg.element(pkg.related(RT["styles"])))
    assert {normalize_style_name(s.name) for s in graph.styles.values()} <= \
        {"normal", "default paragraph font", "normal table", "no list"}


def test_conversion_does_not_change_what_the_document_says(corpora):
    from formgen.oox.walk import Walker

    def words(path):
        return [b.text for b in Walker(OpcPackage.open(path)).blocks()
                if b.is_paragraph and b.text.strip()]

    assert words(corpora["styled"][0]) == words(corpora["unstyled"][0])


# -- recovering the format -----------------------------------------------


def test_a_style_keyed_ballot_alone_learns_only_the_body(learned, tmp_path):
    """The gap this exists to close: with every paragraph Normal, the title,
    the headings and the body collapse into one bucket."""
    rules = json.loads((tmp_path / "p-unstyled" / "profile.json").read_text())["rules"]
    buckets = {p.split("/")[2] for p in rules if p.startswith("/rendered/")}
    assert buckets == {"normal"}


def test_roles_are_learned_from_appearance_when_styles_are_gone(learned, tmp_path):
    sizes = {}
    for label in ("styled", "unstyled"):
        rules = json.loads(
            (tmp_path / f"p-{label}" / "profile.json").read_text())["rules"]
        sizes[label] = {
            role: rules[f"/roles/{role}/run/size"]["value"]
            for role in ("title", "heading1", "body")
            if f"/roles/{role}/run/size" in rules
        }
    assert sizes["unstyled"] == sizes["styled"]
    assert sizes["unstyled"]["title"] != sizes["unstyled"]["body"]


def test_a_guess_is_not_a_ballot():
    """Only classifications confident enough not to be flagged may vote."""
    from formgen.classify.rules import classify_document
    from formgen.learn.observe import observe

    pkg = convert.flatten(build.make(exemplar(0)))
    results = classify_document(build_context(pkg))
    confident = sum(1 for c in results.values()
                    if not c.needs_review and c.role != "empty")
    assert observe(pkg, "x").meta.roles_classified == confident


# -- making the donor able to express it ---------------------------------


def test_the_donor_gains_a_style_for_every_role_it_lacked(learned):
    report = learned["unstyled"].role_styles
    assert set(report.added) >= {"Title", "Heading 1"}
    assert report.mapping["heading1"] == "Heading 1"


def test_a_style_the_donor_already_has_is_never_rebuilt(learned):
    """A real style carries conditional formatting, latent flags and linked
    character styles that no reconstruction from JSON could reproduce."""
    report = learned["styled"].role_styles
    assert report.added == []
    assert "Heading 1" in report.already_defined


def test_the_materialized_style_carries_what_the_corpus_agreed(learned, tmp_path):
    pkg = OpcPackage.open(tmp_path / "p-unstyled" / "template.docx")
    root = pkg.element(pkg.related(RT["styles"]))
    heading = next(s for s in root.findall(qn("w:style"))
                   if s.find(qn("w:name")).get(qn("w:val")) == "Heading 1")
    assert heading.find(f"{qn('w:rPr')}/{qn('w:sz')}").get(qn("w:val")) == "32"
    assert heading.find(f"{qn('w:pPr')}/{qn('w:outlineLvl')}").get(qn("w:val")) == "0"
    # Explicit, never a bare toggle: an inherited <w:b/> turns itself off.
    assert heading.find(f"{qn('w:rPr')}/{qn('w:b')}").get(qn("w:val")) == "1"


def test_the_template_is_still_sound_after_styles_are_added(learned, tmp_path):
    pkg = OpcPackage.open(tmp_path / "p-unstyled" / "template.docx")
    assert pkg.check() == []
    assert pkg.dangling_rels() == []


def test_learn_says_it_read_the_format_from_appearance(learned):
    assert any("read from how they look" in note
               for note in learned["unstyled"].notes)


def test_the_role_to_style_mapping_is_recorded_not_assumed(tmp_path, learned):
    """A w:pStyle naming a style the donor does not define silently takes
    Word's own built-in definition, which differs by version and locale."""
    profile = Profile.load(tmp_path / "p-unstyled")
    mapping = _role_to_style(profile)
    pkg = OpcPackage.open(profile.template)
    graph = StyleGraph.parse(pkg.element(pkg.related(RT["styles"])))
    defined = {normalize_style_name(s.name) for s in graph.styles.values()}
    assert mapping["heading1"] == "heading 1"
    assert all(normalize_style_name(n) in defined for n in mapping.values())


def test_the_style_name_for_a_role_reads_back_as_that_role():
    """The mapping has to round-trip, or re-learning would classify our own
    donor differently from the corpus it came from."""
    from formgen.classify.rules import role_for_style_name

    for role in ("title", "body", "caption", "heading1", "heading3"):
        name = style_name_for(role)
        assert role_for_style_name(normalize_style_name(name)) == role


# -- and applying it -----------------------------------------------------


def test_a_format_learned_without_styles_can_still_be_applied(tmp_path, learned):
    profile = Profile.load(tmp_path / "p-unstyled")
    foreign = build.make(
        build.para("Radiator Qualification Review", rpr='<w:sz w:val="56"/><w:b/>')
        + build.para("Introduction", rpr='<w:sz w:val="32"/><w:b/>',
                     ppr_extra="<w:keepNext/>")
        + build.para("The article met its margin under worst-case loading.")
    )
    plan = build_plan(foreign, profile, "foreign.docx")
    execute(foreign, OpcPackage.open(profile.template), plan)
    assert check_integrity(foreign) == []

    styled = {f.text[:12]: f.style_name for f in build_context(foreign).features
              if f.is_paragraph and f.text.strip()}
    assert styled["Radiator Qua"] == "title"
    assert styled["Introduction"] == "heading 1"
