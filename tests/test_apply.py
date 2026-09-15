"""apply: the graft, the strip rules, the safety kit and the invariants."""

from __future__ import annotations

import hashlib

import pytest
from click.testing import CliRunner
from lxml import etree

from fixtures import build
from formgen.cli import cli
from formgen.errors import RefusalError, UsageError
from formgen.learn.pipeline import learn
from formgen.opc.ns import RT, qn
from formgen.opc.package import OpcPackage
from formgen.plan.builder import build_plan
from formgen.plan.execute import RESTYLE, execute
from formgen.profile.io import Profile
from formgen.safety import backup as backup_mod
from formgen.safety import guards
from formgen.safety.verify import check_integrity, snapshot, verify
from formgen.transform.strip import keep_set, span_scope, strip_paragraph, strip_runs

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def para(text, sz=24, bold=False, keep_next=False):
    return build.para(
        text, rpr=f'<w:sz w:val="{sz}"/>' + ("<w:b/>" if bold else ""),
        ppr_extra="<w:keepNext/>" if keep_next else "",
    )


FOREIGN = (
    para("Thermal Margin Analysis", 56, bold=True)
    + para("Introduction", 32, bold=True, keep_next=True)
    + para("The X-7 radiator exceeds its design margin under worst-case load.")
    + para("The panel was soaked at 340 K for six hours before measurement.")
)


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


def reformat(pkg, profile, **kwargs):
    before = snapshot(pkg)
    plan = build_plan(pkg, profile, document_name="doc.docx")
    report = execute(pkg, OpcPackage.open(profile.template), plan, **kwargs)
    faults = verify(before, pkg,
                    allowed_gains={"comment anchors": report.comments_added})
    return plan, report, faults


def P(inner: str) -> etree._Element:
    return etree.fromstring(f"<w:p {W}>{inner}</w:p>".encode())


def R(text: str, rpr: str = "") -> str:
    return f'<w:r>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t>{text}</w:t></w:r>'


# -- the span-scope rule --------------------------------------------------


def test_bold_over_a_whole_paragraph_is_presentational():
    paragraph = P(R("A WHOLE BOLD HEADING", "<w:b/><w:sz w:val='32'/>"))
    assert span_scope(paragraph, "w:b") == "all"
    strip_runs(paragraph)
    assert paragraph.find(f".//{qn('w:b')}") is None


def test_italic_over_a_sub_span_is_emphasis_and_survives():
    """"the *p*-value was significant" must keep its emphasis."""
    paragraph = P(R("the ") + R("p", "<w:i/>") + R("-value was significant"))
    assert span_scope(paragraph, "w:i") == "some"
    stats = strip_runs(paragraph)
    assert paragraph.find(f".//{qn('w:i')}") is not None
    assert stats.emphasis_kept == 1


def test_a_font_change_is_dropped_even_over_a_sub_span():
    """Only {b, i, u, strike} are emphasis; a typeface is never a writing
    decision."""
    paragraph = P(R("normal ") + R("odd", "<w:rFonts w:ascii='Courier New'/>"))
    strip_runs(paragraph)
    assert paragraph.find(f".//{qn('w:rFonts')}") is None


def test_rstyle_survives_so_hyperlinks_stay_links():
    paragraph = P(R(
        "link", "<w:rStyle w:val='Hyperlink'/><w:color w:val='0563C1'/>"
        "<w:u w:val='single'/>"
    ))
    strip_runs(paragraph)
    assert paragraph.find(f".//{qn('w:rStyle')}") is not None
    assert paragraph.find(f".//{qn('w:color')}") is None


def test_superscript_survives_because_it_is_notation():
    paragraph = P(R("E=mc") + R("2", "<w:vertAlign w:val='superscript'/>"))
    strip_runs(paragraph)
    assert paragraph.find(f".//{qn('w:vertAlign')}") is not None


def test_an_empty_rpr_is_removed_entirely():
    paragraph = P(R("text", "<w:sz w:val='28'/>"))
    strip_runs(paragraph)
    assert paragraph.find(f".//{qn('w:rPr')}") is None


def test_the_paragraph_mark_rpr_is_not_treated_as_a_run():
    paragraph = P("<w:pPr><w:rPr><w:sz w:val='28'/></w:rPr></w:pPr>" + R("x"))
    stats = strip_runs(paragraph)
    assert stats.runs_touched == 0


def test_runs_are_never_rebuilt():
    """The invariant that keeps fields, bookmarks and comment ranges alive."""
    paragraph = P(
        '<w:bookmarkStart w:id="1" w:name="x"/>'
        + R("before", "<w:sz w:val='28'/>")
        + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        + '<w:bookmarkEnd w:id="1"/>'
    )
    tags_before = [etree.QName(c).localname for c in paragraph]
    strip_runs(paragraph)
    assert [etree.QName(c).localname for c in paragraph] == tags_before


# -- paragraph properties -------------------------------------------------


def test_ppr_is_rebuilt_from_what_we_keep_not_pruned():
    paragraph = P(
        '<w:pPr><w:jc w:val="center"/><w:spacing w:after="240"/>'
        '<w:shd w:fill="FFFF00"/><w:pageBreakBefore/></w:pPr>' + R("x")
    )
    strip_paragraph(paragraph, "BodyText", keep_set(paragraph))
    ppr = paragraph.find(qn("w:pPr"))
    assert [etree.QName(c).localname for c in ppr] == ["pStyle", "pageBreakBefore"]


def test_tabs_are_kept_only_when_the_paragraph_actually_uses_one():
    """Signature blocks and leader-dot lines break catastrophically without."""
    signature = P(
        '<w:pPr><w:tabs><w:tab w:val="right" w:pos="9360"/></w:tabs></w:pPr>'
        + R("Approved by:") + "<w:r><w:tab/></w:r>" + R("Date")
    )
    strip_paragraph(signature, "BodyText", keep_set(signature))
    assert signature.find(f"{qn('w:pPr')}/{qn('w:tabs')}") is not None

    ordinary = P(
        '<w:pPr><w:tabs><w:tab w:val="left" w:pos="720"/></w:tabs></w:pPr>'
        + R("just prose")
    )
    strip_paragraph(ordinary, "BodyText", keep_set(ordinary))
    assert ordinary.find(f"{qn('w:pPr')}/{qn('w:tabs')}") is None


def test_sectpr_is_always_kept():
    """Dropping it merges two sections and silently changes the page setup of
    everything before it."""
    paragraph = P('<w:pPr><w:jc w:val="left"/><w:sectPr/></w:pPr>' + R("x"))
    strip_paragraph(paragraph, "BodyText", keep_set(paragraph))
    assert paragraph.find(f"{qn('w:pPr')}/{qn('w:sectPr')}") is not None


def test_the_rebuilt_ppr_is_in_schema_order():
    """CT_PPr is a sequence; the wrong order is a file Word offers to repair."""
    paragraph = P(
        '<w:pPr><w:sectPr/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/>'
        '</w:numPr><w:pageBreakBefore/></w:pPr>' + R("x") + "<w:r><w:tab/></w:r>"
    )
    strip_paragraph(paragraph, "BodyText", keep_set(paragraph, numbered=True))
    order = [etree.QName(c).localname for c in paragraph.find(qn("w:pPr"))]
    assert order == ["pStyle", "pageBreakBefore", "numPr", "sectPr"]


# -- the graft ------------------------------------------------------------


def test_the_format_parts_come_from_the_profile(profile):
    pkg = build.make(body=FOREIGN)
    reformat(pkg, profile)
    donor = OpcPackage.open(profile.template)
    assert pkg.blob(pkg.related(RT["styles"])) == \
        donor.blob(donor.related(RT["styles"]))
    assert pkg.blob(pkg.related(RT["theme"])) == \
        donor.blob(donor.related(RT["theme"]))


def test_page_setup_is_re_set_from_the_profile(profile):
    pkg = build.make(body=FOREIGN, sect_pr=build.sectpr(
        margins='w:top="1440" w:right="1080" w:bottom="1440" w:left="1080"'
    ))
    reformat(pkg, profile)
    margins = pkg.element(pkg.main_document).find(
        f"{qn('w:body')}/{qn('w:sectPr')}/{qn('w:pgMar')}"
    )
    assert margins.get(qn("w:left")) == "1440"


def test_settings_are_carried_in_part_not_wholesale(profile):
    """settings.xml mixes format with document state; replacing it would take
    the donor's revision ids and document variables with it."""
    pkg = build.make(body=FOREIGN,
                     settings_extra='<w:docVars><w:docVar w:name="keep" '
                                    'w:val="me"/></w:docVars>')
    reformat(pkg, profile)
    settings = pkg.blob(pkg.related(RT["settings"])).decode()
    assert 'w:name="keep"' in settings
    assert 'w:updateFields w:val="true"' in settings


def test_settings_children_stay_in_schema_order(profile):
    pkg = build.make(body=FOREIGN)
    reformat(pkg, profile)
    root = pkg.element(pkg.related(RT["settings"]))
    names = [etree.QName(c).localname for c in root]
    assert names.index("defaultTabStop") < names.index("updateFields")
    assert names.index("updateFields") < names.index("compat")


def test_a_grafted_header_brings_its_own_parts_and_relationships(tmp_path):
    source = build.make()
    rid = build.add_hdrftr(source, "header", "header1.xml", "HOUSE REPORT")
    sect = source.edit(source.main_document).find(f"{qn('w:body')}/{qn('w:sectPr')}")
    reference = etree.Element(qn("w:headerReference"))
    reference.set(qn("w:type"), "default")
    reference.set(qn("r:id"), rid)
    sect.insert(0, reference)
    directory = tmp_path / "hdr"
    directory.mkdir()
    for i in range(5):
        source.save(directory / f"h{i}.docx", deterministic=True)
    learn(sorted(directory.glob("*.docx")), tmp_path / "hp")
    profile = Profile.load(tmp_path / "hp")

    pkg = build.make(body=FOREIGN)
    _, report, faults = reformat(pkg, profile)
    assert faults == []
    headers = pkg.related_all(RT["header"])
    assert headers
    assert "HOUSE REPORT" in pkg.blob(headers[0]).decode()
    body_sect = pkg.element(pkg.main_document).find(
        f"{qn('w:body')}/{qn('w:sectPr')}"
    )
    assert body_sect.find(qn("w:headerReference")) is not None


def test_a_copied_part_keeps_its_own_relationship_ids(tmp_path):
    """Keeping the rIds means the copied XML is never rewritten, and not
    rewriting a customer's XML is always the safer option."""
    from formgen.transform.graft import GraftReport, copy_part

    donor = build.make()
    build.add_hdrftr(donor, "header", "header1.xml", "H")
    donor.add_part("word/media/logo.png", b"\x89PNG\r\n\x1a\n", "image/png")
    donor.touch_rels("word/header1.xml").put(
        "rId7", RT["image"], "media/logo.png"
    )
    donor.save(tmp_path / "donor.docx", deterministic=True)
    donor = OpcPackage.open(tmp_path / "donor.docx")

    target = build.make()
    report = GraftReport()
    name = copy_part(donor, target, "word/header1.xml", "word/header{n}.xml", report)
    rels = target.rels(name)
    assert "rId7" in rels
    assert target.dangling_rels() == []


# -- identifier remapping -------------------------------------------------


def test_style_references_are_remapped_by_name_not_by_id(profile, tmp_path):
    """w:styleId is localised for built-ins; w:name is not."""
    styles = [
        build.style("Ueberschrift1", "heading 1", outline=0,
                    rpr='<w:sz w:val="32"/><w:b/>'),
        build.style("Standardabsatz", "Body Text"),
    ]
    pkg = build.make(
        body=build.para("Einleitung", style="Ueberschrift1")
        + build.para("Ein Absatz, der ordentlich endet und lang genug ist.",
                     style="Standardabsatz"),
        styles=styles,
    )
    _, report, faults = reformat(pkg, profile)
    assert faults == []
    document = pkg.blob(pkg.main_document).decode()
    assert "Ueberschrift1" not in document
    assert 'w:val="Heading1"' in document


def test_an_unmatched_character_style_is_dropped_rather_than_left_dangling(profile):
    pkg = build.make(body=(
        f'<w:p {W}><w:r><w:rPr><w:rStyle w:val="SomethingBespoke"/></w:rPr>'
        "<w:t>styled</w:t></w:r></w:p>"
    ), styles=list(build.DEFAULT_STYLES) + [
        build.style("SomethingBespoke", "Something Bespoke", type="character",
                    based_on=None, rpr='<w:b/>')
    ])
    _, report, faults = reformat(pkg, profile)
    assert faults == []
    assert "SomethingBespoke" not in pkg.blob(pkg.main_document).decode()


def test_a_source_list_matching_the_profile_reuses_the_profile_definition(profile):
    pkg = build.make(body=build.para("An item", numid=1))
    _, report, faults = reformat(pkg, profile)
    assert faults == []
    assert report.lists_reused >= 1


def test_two_numbered_lists_do_not_merge_into_one(profile):
    """Sharing a w:num makes the second list continue the first's numbering
    instead of restarting at 1 -- a bug that ships in most docx generators."""
    numbering = build._NUMBERING.replace(
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>',
        '<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
        '<w:num w:numId="3"><w:abstractNumId w:val="1"/></w:num>',
    )
    pkg = build.make(
        body=build.para("first list", numid=2) + build.para("second list", numid=3),
        numbering=numbering,
    )
    _, report, faults = reformat(pkg, profile)
    assert faults == []
    root = pkg.element(pkg.related(RT["numbering"]))
    used = {
        el.get(qn("w:val"))
        for el in pkg.element(pkg.main_document).findall(f".//{qn('w:numId')}")
    }
    assert len(used) == 2, "the two lists must not share one w:num"
    for num in root.findall(qn("w:num")):
        if num.get(qn("w:numId")) in used:
            assert num.find(f"{qn('w:lvlOverride')}/{qn('w:startOverride')}") \
                is not None


def test_imported_lists_get_a_fresh_nsid():
    """A duplicate w:nsid makes Word merge two unrelated lists."""
    from formgen.oox.numbering import Numbering
    from formgen.transform.remap import merge_numbering

    donor_root = etree.fromstring(build._NUMBERING.encode())
    source_root = etree.fromstring(build._NUMBERING.replace(
        '<w:lvlText w:val="%1."/>', '<w:lvlText w:val="(%1)"/>'
    ).encode())
    merge = merge_numbering(
        donor_root, Numbering.parse(source_root), {2},
        Numbering.parse(donor_root),
    )
    nsids = [
        el.get(qn("w:val"))
        for a in merge.root.findall(qn("w:abstractNum"))
        for el in a.findall(qn("w:nsid"))
    ]
    assert len(nsids) == len(set(nsids)), nsids
    assert merge.added == 1


def test_numid_zero_is_never_remapped():
    from formgen.transform.remap import apply_num_map

    root = etree.fromstring(
        f'<w:p {W}><w:pPr><w:numPr><w:numId w:val="0"/></w:numPr></w:pPr></w:p>'
        .encode()
    )
    apply_num_map(root, {0: 5, 1: 9})
    assert root.find(f".//{qn('w:numId')}").get(qn("w:val")) == "0"


# -- invariants -----------------------------------------------------------


def test_nothing_is_lost(profile):
    body = (
        FOREIGN
        + '<w:p><w:hyperlink r:id="rId1" xmlns:r="http://schemas.'
          'openxmlformats.org/officeDocument/2006/relationships">'
          "<w:r><w:t>a link</w:t></w:r></w:hyperlink></w:p>"
        + '<w:p><w:bookmarkStart w:id="1" w:name="fig"/>'
          "<w:r><w:t>anchored</w:t></w:r>"
          '<w:bookmarkEnd w:id="1"/></w:p>'
        + "<w:tbl><w:tr><w:tc>" + build.para("in a cell") + "</w:tc></w:tr></w:tbl>"
    )
    pkg = build.make(body=body)
    before = snapshot(pkg)
    _, report, faults = reformat(pkg, profile)
    assert faults == []
    after = snapshot(pkg)
    assert after.counts["hyperlinks"] == before.counts["hyperlinks"]
    assert after.counts["bookmarks"] == before.counts["bookmarks"]
    assert "in a cell" in after.joined


def test_apply_is_idempotent(profile, tmp_path):
    """apply(apply(x)) == apply(x): an excellent detector for strip and remap
    bugs, because either one that is not a fixed point shows up here."""
    pkg = build.make(body=FOREIGN)
    reformat(pkg, profile)
    pkg.save(tmp_path / "once.docx", deterministic=True)

    again = OpcPackage.open(tmp_path / "once.docx")
    reformat(again, profile)
    again.save(tmp_path / "twice.docx", deterministic=True)

    assert hashlib.sha256((tmp_path / "once.docx").read_bytes()).hexdigest() == \
        hashlib.sha256((tmp_path / "twice.docx").read_bytes()).hexdigest()


def test_lint_of_an_applied_document_is_clean(profile):
    """The property that justifies the whole design: the linter and the
    reformatter agree because they share an engine."""
    pkg = build.make(body=FOREIGN)
    reformat(pkg, profile)
    assert build_plan(pkg, profile, document_name="x").findings == []


def test_the_output_is_referentially_intact(profile):
    pkg = build.make(body=FOREIGN)
    reformat(pkg, profile)
    assert check_integrity(pkg) == []


def test_verify_catches_invented_text():
    from formgen.safety.verify import Snapshot, compare

    before = Snapshot(text=["a", "b"], counts={"images": 1})
    after = Snapshot(text=["a", "b", "c"], counts={"images": 1})
    assert any("invented" in f for f in compare(before, after))


def test_verify_catches_a_lost_hyperlink():
    from formgen.safety.verify import Snapshot, compare

    before = Snapshot(text=["a"], counts={"hyperlinks": 3})
    after = Snapshot(text=["a"], counts={"hyperlinks": 1})
    assert compare(before, after) == ["lost 2 of 3 hyperlinks"]


def test_a_deliberate_gain_is_allowed_but_an_accidental_one_is_not():
    from formgen.safety.verify import Snapshot, compare

    before = Snapshot(counts={"comment anchors": 0, "images": 1})
    after = Snapshot(counts={"comment anchors": 3, "images": 4})
    faults = compare(before, after, allowed_gains={"comment anchors": 3})
    assert faults == ["gained 3 images (was 1, now 4)"]


def test_a_paragraph_after_the_body_sectpr_is_caught():
    pkg = build.make()
    body = pkg.edit(pkg.main_document).find(qn("w:body"))
    etree.SubElement(body, qn("w:p"))
    assert any("follow the body-level w:sectPr" in f for f in check_integrity(pkg))


# -- low confidence and the review loop -----------------------------------


def test_a_low_confidence_paragraph_is_left_alone_by_default(profile):
    pkg = build.make(body=build.para("?"))
    plan, report, faults = reformat(pkg, profile)
    assert faults == []
    if plan.needs_review:
        assert report.edits_skipped == len(plan.needs_review)


def test_restyle_mode_acts_on_the_guesses_instead(profile):
    pkg = build.make(body=build.para("?"))
    plan, report, _ = reformat(pkg, profile, on_low_confidence=RESTYLE)
    assert report.edits_skipped == 0


def test_mark_uncertain_anchors_a_real_word_comment(profile):
    """With Word present this turns review into Next Comment, instead of
    hunting for find strings in a report."""
    pkg = build.make(body=build.para("?"))
    plan, report, faults = reformat(pkg, profile, mark_uncertain=True)
    assert faults == []
    if plan.needs_review:
        assert report.comments_added == len(plan.needs_review)
        comments = pkg.related(RT["comments"])
        assert comments and "formgen read this as" in pkg.blob(comments).decode()
        assert check_integrity(pkg) == []


# -- the safety kit -------------------------------------------------------


def test_the_input_is_never_the_default_output(tmp_path):
    source = tmp_path / "report.docx"
    assert guards.output_path(source).name == "report.formatted.docx"
    assert guards.output_path(source, out_dir=tmp_path / "out") == \
        tmp_path / "out" / "report.docx"
    assert guards.output_path(source, in_place=True) == source


def test_a_document_open_in_word_is_refused(tmp_path):
    source = tmp_path / "report.docx"
    source.write_bytes(b"x")
    (tmp_path / "~$port.docx").write_bytes(b"lock")
    with pytest.raises(RefusalError) as excinfo:
        guards.check_source(source)
    assert "close it first" in excinfo.value.remedy


def test_in_place_refuses_without_a_terminal(tmp_path):
    destination = tmp_path / "out.docx"
    with pytest.raises(UsageError) as excinfo:
        guards.check_writable(destination, in_place=True, interactive=False)
    assert "--force" in excinfo.value.remedy
    guards.check_writable(destination, in_place=True, force=True, interactive=False)


def test_undo_restores_the_original(tmp_path):
    original = tmp_path / "doc.docx"
    build.make().save(original, deterministic=True)
    first = original.read_bytes()

    saved = backup_mod.make_backup(original, when="20260915-120000")
    build.make(body=build.para("changed")).save(original, deterministic=True)
    backup_mod.write_manifest(original, original, saved, profile="p")

    backup_mod.undo(original)
    assert original.read_bytes() == first


def test_undo_refuses_when_the_user_has_edited_since(tmp_path):
    """An undo that can destroy work is worse than no undo, because people
    trust it."""
    original = tmp_path / "doc.docx"
    build.make().save(original, deterministic=True)
    saved = backup_mod.make_backup(original, when="20260915-120000")
    backup_mod.write_manifest(original, original, saved)

    build.make(body=build.para("their later edit")).save(original, deterministic=True)
    with pytest.raises(RefusalError) as excinfo:
        backup_mod.undo(original)
    assert "would discard those edits" in excinfo.value.remedy


def test_undo_without_a_manifest_is_a_usage_error(tmp_path):
    path = tmp_path / "doc.docx"
    path.write_bytes(b"x")
    with pytest.raises(UsageError):
        backup_mod.undo(path)


# -- the CLI --------------------------------------------------------------


def cli_profile(runner, tmp_path):
    paths = [str(p) for p in corpus(tmp_path / "c")]
    runner.invoke(cli, ["learn", *paths, "-o", str(tmp_path / "p")], obj={})
    return str(tmp_path / "p")


def test_apply_writes_beside_the_input_by_default(tmp_path):
    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    source = tmp_path / "foreign.docx"
    build.make(body=FOREIGN).save(source, deterministic=True)
    original = source.read_bytes()

    result = runner.invoke(
        cli, ["apply", str(source), "-p", directory, "--no-mark-uncertain"], obj={}
    )
    assert result.exit_code == 0, result.output
    assert source.read_bytes() == original           # input untouched
    assert (tmp_path / "foreign.formatted.docx").exists()


def test_dry_run_writes_nothing_and_renders_the_lint_report(tmp_path):
    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    source = tmp_path / "foreign.docx"
    build.make(body=FOREIGN).save(source, deterministic=True)
    result = runner.invoke(
        cli, ["apply", str(source), "-p", directory, "--dry-run"], obj={}
    )
    assert result.exit_code == 0
    assert "--dry-run: nothing written" in result.output
    assert "ERRORS" in result.output
    assert not (tmp_path / "foreign.formatted.docx").exists()


def test_apply_refuses_a_document_with_tracked_changes(tmp_path):
    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    source = tmp_path / "tracked.docx"
    build.make(body="<w:ins>" + build.para("x") + "</w:ins>").save(
        source, deterministic=True
    )
    result = runner.invoke(cli, ["apply", str(source), "-p", directory], obj={})
    assert result.exit_code == 3
    assert "REFUSED" in result.output
    assert not (tmp_path / "tracked.formatted.docx").exists()


def test_in_place_keeps_a_backup_and_undo_puts_it_back(tmp_path):
    runner = CliRunner()
    directory = cli_profile(runner, tmp_path)
    source = tmp_path / "doc.docx"
    build.make(body=FOREIGN).save(source, deterministic=True)
    original = source.read_bytes()

    result = runner.invoke(
        cli, ["apply", str(source), "-p", directory, "--in-place", "--force",
              "--no-mark-uncertain"], obj={},
    )
    assert result.exit_code == 0, result.output
    assert source.read_bytes() != original

    undone = runner.invoke(cli, ["undo", str(source)], obj={})
    assert undone.exit_code == 0
    assert source.read_bytes() == original
