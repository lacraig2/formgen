"""learn -> profile -> correct in Word -> sync -> re-learn, end to end."""

from __future__ import annotations

import json

import pytest
from lxml import etree

from fixtures import build
from formgen.errors import RefusalError
from formgen.learn import consensus as C
from formgen.learn import donor as D
from formgen.learn.observe import observe
from formgen.learn.pipeline import learn
from formgen.opc.ns import RT, qn
from formgen.opc.package import OpcPackage
from formgen.profile import io as pio
from formgen.profile.schema import decode, encode
from formgen.profile.sync import placeholders_in, sync
from formgen.oox.values import FontSize, Length

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def corpus(tmp_path, n=6, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        path = tmp_path / f"report{i}.docx"
        build.make(**kwargs).save(path, deterministic=True)
        paths.append(path)
    return paths


# -- observation ----------------------------------------------------------


def test_observation_reports_resolved_values_not_raw_ones():
    """docDefaults carries the size and Normal is empty; Body Text must still
    report 11pt, or two documents that made the same decision differently
    would look as though they disagreed."""
    obs = observe(build.make(), "a")
    assert obs.values["/styles/paragraph/body text/run/size"] == [FontSize.pt(11)]
    assert obs.values["/defaults/run/size"] == [FontSize.pt(11)]


def test_only_styles_the_document_uses_get_a_vote():
    """A file carrying Word's latent style table has not thereby voted."""
    obs = observe(build.make(), "a")
    assert "caption" not in obs.meta.styles_used
    assert not any(p.startswith("/styles/paragraph/caption/") for p in obs.values)
    assert "body text" in obs.meta.styles_used


def test_direct_formatting_density_is_measured():
    body = (
        build.para("plain", style="BodyText")
        + build.para("fought", style="BodyText", rpr='<w:sz w:val="28"/>')
    )
    obs = observe(build.make(body=body), "a")
    assert obs.meta.runs == 2
    assert obs.meta.direct_runs == 1
    assert obs.meta.direct_density == pytest.approx(0.5)


def test_the_paragraph_mark_rpr_is_not_counted_as_a_run():
    """Word writes w:pPr/w:rPr constantly; counting it reports direct
    formatting in every document ever saved."""
    body = "<w:p><w:pPr><w:rPr><w:sz w:val='28'/></w:rPr></w:pPr>" \
           "<w:r><w:t>x</w:t></w:r></w:p>"
    obs = observe(build.make(body=body), "a")
    assert obs.meta.runs == 1
    assert obs.meta.direct_runs == 0


def test_revisions_in_the_body_count_even_with_track_changes_off():
    body = "<w:ins><w:p><w:r><w:t>added</w:t></w:r></w:p></w:ins>"
    obs = observe(build.make(body=body), "a")
    assert obs.meta.has_tracked_changes is True


def test_the_primary_section_is_the_one_holding_the_body():
    """Section index is a poor join key: a cover is a section in some
    exemplars and not in others."""
    body = (
        build.para("COVER")
        + build.break_para(build.sectpr(w=15840, h=12240))   # landscape cover
        + "".join(build.para(f"body {i}", style="BodyText") for i in range(6))
    )
    obs = observe(build.make(body=body), "a")
    assert obs.values["/page/primary/orientation"] == ["portrait"]


# -- consensus ------------------------------------------------------------


def test_a_dissenting_document_is_named_and_the_majority_wins():
    docs = [observe(build.make(default_sz=22), f"d{i}") for i in range(5)]
    docs.append(observe(build.make(default_sz=24), "odd"))
    result = C.build(docs)
    vote = result.votes["/defaults/run/size"]
    assert vote.value == FontSize.pt(11)
    assert vote.dissenting == ("odd",)
    assert vote.status == "contested"


def test_two_exemplars_produce_a_loud_warning():
    result = C.build([observe(build.make(), f"d{i}") for i in range(2)])
    assert any("only 2 exemplar" in w for w in result.warnings)


def test_a_corpus_that_is_really_two_formats_says_so():
    docs = [observe(build.make(default_sz=22, minor="Calibri"), f"old{i}")
            for i in range(3)]
    docs += [observe(build.make(default_sz=24, minor="Aptos"), f"new{i}")
             for i in range(3)]
    result = C.build(docs)
    assert result.split is not None
    a, b = result.split.clusters
    assert {len(a), len(b)} == {3}
    assert any("splits into" in w for w in result.warnings)


def test_a_uniform_corpus_is_not_split():
    result = C.build([observe(build.make(), f"d{i}") for i in range(6)])
    assert result.split is None


def test_clustering_is_deterministic_whatever_the_input_order():
    def run(order):
        docs = [observe(build.make(default_sz=22), f"a{i}") for i in range(3)]
        docs += [observe(build.make(default_sz=30), f"b{i}") for i in range(3)]
        return C.build([docs[i] for i in order])

    forward = run(range(6))
    reverse = run(list(reversed(range(6))))
    assert forward.split is not None and reverse.split is not None
    assert {tuple(sorted(c.members)) for c in forward.split.clusters} == \
           {tuple(sorted(c.members)) for c in reverse.split.clusters}


# -- donor selection ------------------------------------------------------


def test_the_document_that_fought_its_styles_loses_to_a_plainer_one():
    """The exemplar that looks most correct is often the worst template."""
    clean_body = "".join(
        build.para(f"para {i}", style="BodyText") for i in range(12)
    )
    messy_body = "".join(
        build.para(f"para {i}", style="BodyText",
                   rpr='<w:rFonts w:ascii="Arial"/><w:sz w:val="22"/>')
        for i in range(12)
    )
    docs = [observe(build.make(body=clean_body), "clean")]
    docs += [observe(build.make(body=messy_body), f"messy{i}") for i in range(3)]
    result = C.build(docs)
    ranked = D.rank(docs, result)
    assert ranked[0].doc == "clean"
    assert ranked[0].direct_density == 0.0
    assert ranked[-1].direct_density == 1.0


def test_a_protected_document_is_disqualified_outright():
    docs = [observe(build.make(), "ok")]
    docs.append(observe(
        build.make(settings_extra='<w:documentProtection w:edit="readOnly" '
                                  'w:enforcement="1"/>'),
        "locked",
    ))
    result = C.build(docs)
    scores = {s.doc: s for s in D.rank(docs, result)}
    assert scores["locked"].total == float("-inf")
    assert "protection is enforced" in scores["locked"].disqualified[0]
    assert scores["ok"].disqualified == ()


def test_learn_refuses_when_every_candidate_is_disqualified(tmp_path):
    paths = corpus(tmp_path, 3, settings_extra="<w:trackChanges/>")
    with pytest.raises(RefusalError) as excinfo:
        learn(paths, tmp_path / "prof")
    assert "no exemplar can serve as the donor" in excinfo.value.message
    assert "track changes is turned on" in excinfo.value.message
    assert excinfo.value.exit_code == 3


# -- the scrub ------------------------------------------------------------


def test_scrub_removes_identity_session_state_and_hostile_settings():
    pkg = build.make(
        body='<w:p w:rsidR="00AB12CD" w:rsidRDefault="00AB12CD">'
             "<w:r><w:t>x</w:t></w:r></w:p>",
        settings_extra='<w:proofState w:spelling="clean"/>'
                       '<w:documentProtection w:edit="forms" w:enforcement="1"/>'
                       '<w:docVars><w:docVar w:name="x" w:val="y"/></w:docVars>',
        creator="Someone Real",
    )
    report = D.scrub(pkg)
    settings = pkg.blob(pkg.related(RT["settings"])).decode()
    assert "proofState" not in settings
    assert "documentProtection" not in settings
    assert "docVars" not in settings
    assert 'w:updateFields w:val="true"' in settings

    document = pkg.blob(pkg.main_document).decode()
    assert "rsidR" not in document
    core = pkg.blob(pkg.related(RT["core"], source="")).decode()
    assert "Someone Real" not in core
    assert report.removed["editing-session ids"] == 2


def test_scrub_drops_comment_parts_and_their_anchors():
    comments = (
        '<?xml version="1.0"?>'
        f'<w:comments xmlns:w="{W_NS}"><w:comment w:id="1"><w:p/></w:comment>'
        "</w:comments>"
    )
    pkg = build.make(
        body='<w:p><w:commentRangeStart w:id="1"/><w:r><w:t>x</w:t></w:r>'
             '<w:commentRangeEnd w:id="1"/>'
             '<w:r><w:commentReference w:id="1"/></w:r></w:p>'
    )
    pkg.add_part("word/comments.xml", comments.encode(),
                 "application/vnd.openxmlformats-officedocument."
                 "wordprocessingml.comments+xml")
    pkg.relate(RT["comments"], "word/comments.xml", pkg.main_document)

    D.scrub(pkg)
    assert "word/comments.xml" not in pkg
    document = pkg.blob(pkg.main_document).decode()
    assert "commentRangeStart" not in document
    assert "commentReference" not in document
    # The anchor run went with it, so nothing counts a phantom run later.
    assert document.count("<w:r>") == 1


def test_scrub_unlocks_content_controls():
    """A locked control bounces the user's own edits, which is the one thing
    the correction workflow cannot survive."""
    sdt = (
        f'<w:sdt xmlns:w="{W_NS}"><w:sdtPr><w:tag w:val="formgen.x"/>'
        '<w:lock w:val="sdtContentLocked"/>'
        '<w:dataBinding w:xpath="/root/x"/></w:sdtPr>'
        "<w:sdtContent><w:p><w:r><w:t>v</w:t></w:r></w:p></w:sdtContent></w:sdt>"
    )
    pkg = build.make(body=sdt)
    D.scrub(pkg)
    document = pkg.blob(pkg.main_document).decode()
    assert "w:lock" not in document
    assert "dataBinding" not in document


def test_a_scrubbed_donor_still_opens():
    pkg = build.make()
    D.scrub(pkg)
    blob = pkg.blob(pkg.main_document)
    assert blob.startswith(b"<?xml")
    assert pkg.dangling_rels() == []


# -- profile artifacts ----------------------------------------------------


def test_learn_writes_every_artifact(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof", generated="2026-01-01T00:00:00")
    directory = result.directory
    for name in (pio.TEMPLATE, pio.PROFILE, pio.EVIDENCE, pio.CORPUS, pio.README):
        assert (directory / name).exists(), name
    profile = json.loads((directory / pio.PROFILE).read_text())
    assert profile["corpus"]["count"] == 6
    assert profile["template"]["sha256"] == result.template_sha
    assert profile["format"]["styles"]["paragraph"]["body text"]["run"]["size"] == "11pt"


def test_the_donor_template_is_a_real_openable_docx(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    pkg = OpcPackage.open(result.directory / pio.TEMPLATE)
    assert pkg.main_document == "word/document.xml"
    assert pkg.dangling_rels() == []


def test_learn_is_reproducible(tmp_path):
    a = learn(corpus(tmp_path / "a"), tmp_path / "pa", generated="T")
    b = learn(corpus(tmp_path / "b"), tmp_path / "pb", generated="T")
    assert a.template_sha == b.template_sha
    assert (a.directory / pio.PROFILE).read_text() == \
           (b.directory / pio.PROFILE).read_text().replace("pb", "pa")


def test_readme_names_the_donor_and_the_warnings(tmp_path):
    result = learn(corpus(tmp_path, 2), tmp_path / "prof")
    readme = (result.directory / pio.README).read_text()
    assert "template.docx` **is** the format" in readme
    assert "only 2 exemplar" in readme
    assert result.donor.doc in readme


# -- the correction loop --------------------------------------------------


def edit_template(directory, *, left_margin=None, heading2_sz=None, add_sdt=False):
    """Stand in for a human opening template.docx in Word."""
    pkg = OpcPackage.open(directory / pio.TEMPLATE)
    if left_margin is not None:
        margins = pkg.edit(pkg.main_document).find(
            f"{qn('w:body')}/{qn('w:sectPr')}/{qn('w:pgMar')}"
        )
        margins.set(qn("w:left"), str(left_margin))
    if heading2_sz is not None:
        styles = pkg.edit(pkg.related(RT["styles"]))
        for style in styles.findall(qn("w:style")):
            if style.get(qn("w:styleId")) == "Heading2":
                style.find(f"{qn('w:rPr')}/{qn('w:sz')}").set(
                    qn("w:val"), str(heading2_sz)
                )
    if add_sdt:
        body = pkg.edit(pkg.main_document).find(qn("w:body"))
        body.insert(0, etree.fromstring(
            f'<w:sdt xmlns:w="{W_NS}"><w:sdtPr>'
            '<w:tag w:val="formgen.report_number"/>'
            '<w:alias w:val="Report Number"/><w:text/></w:sdtPr><w:sdtContent>'
            "<w:p><w:r><w:t>LR-2026-0142</w:t></w:r></w:p>"
            "</w:sdtContent></w:sdt>".encode()
        ))
    pkg.save(directory / pio.TEMPLATE, deterministic=True)


def test_sync_pins_what_a_human_changed_in_word(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    edit_template(result.directory, left_margin=1800)

    report = sync(result.directory, today="2026-09-15")
    assert report.changed is True
    pinned = {d.pointer for d in report.divergences}
    assert "/page/primary/margins/left" in pinned

    overrides = pio.Overrides.load(result.directory / pio.OVERRIDES)
    pin = overrides.pins["/page/primary/margins/left"]
    assert pin.value == Length.inches(1.25)
    assert pin.pinned == "2026-09-15"
    assert pin.source == "sync"


def test_sync_does_not_pin_derived_values(tmp_path):
    """content_width IS page width minus margins; a separate pin for it goes
    stale the moment a margin changes."""
    result = learn(corpus(tmp_path), tmp_path / "prof")
    edit_template(result.directory, left_margin=1800)
    report = sync(result.directory)
    assert "/page/primary/content_width" not in {d.pointer for d in report.divergences}


def test_sync_reads_content_controls_as_placeholders(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    edit_template(result.directory, add_sdt=True)
    report = sync(result.directory)
    assert report.placeholders_added == ("report_number",)
    overrides = pio.Overrides.load(result.directory / pio.OVERRIDES)
    assert overrides.placeholders["report_number"]["label"] == "Report Number"
    assert overrides.placeholders["report_number"]["type"] == "text"


def test_only_our_own_content_controls_are_placeholders():
    """Word's cover-page date picker is not a field we are meant to fill."""
    pkg = build.make(body=(
        f'<w:sdt xmlns:w="{W_NS}"><w:sdtPr><w:tag w:val="CoverDate"/><w:date/>'
        "</w:sdtPr><w:sdtContent><w:p/></w:sdtContent></w:sdt>"
        f'<w:sdt xmlns:w="{W_NS}"><w:sdtPr><w:tag w:val="formgen.author"/>'
        "</w:sdtPr><w:sdtContent><w:p/></w:sdtContent></w:sdt>"
    ))
    assert set(placeholders_in(pkg)) == {"author"}


def test_sync_on_an_untouched_template_changes_nothing(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    report = sync(result.directory)
    assert report.changed is False
    assert report.divergences == []
    assert "unchanged" in report.render()


# -- pins survive re-learning: the point of the whole arrangement ---------


def test_a_pin_survives_relearning_with_a_bigger_corpus(tmp_path):
    result = learn(corpus(tmp_path / "first", 4), tmp_path / "prof")
    edit_template(result.directory, left_margin=1800)
    sync(result.directory, today="2026-09-15")

    again = learn(corpus(tmp_path / "second", 12), tmp_path / "prof")
    profile = json.loads((again.directory / pio.PROFILE).read_text())
    left = profile["rules"]["/page/primary/margins/left"]
    assert left["value"] == "1.25in"     # the pin, not the corpus's 1in
    assert left["pinned"] is True


def test_a_pin_the_corpus_has_caught_up_with_is_reported_as_redundant(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    overrides = pio.Overrides.load(result.directory / pio.OVERRIDES)
    overrides.add_pin("/defaults/run/size", FontSize.pt(11), today="2026-01-01")
    _, outcomes = pio.apply_overrides(result.consensus, overrides)
    assert [o.status for o in outcomes] == ["redundant"]
    assert "redundant, drop it?" in outcomes[0].describe()


def test_a_pin_the_corpus_contradicts_still_wins_and_says_so(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    overrides = pio.Overrides.load(result.directory / pio.OVERRIDES)
    overrides.add_pin("/defaults/run/size", FontSize.pt(13), today="2026-01-01")
    values, outcomes = pio.apply_overrides(result.consensus, overrides)
    assert values["/defaults/run/size"] == FontSize.pt(13)
    assert outcomes[0].status == "overriding"
    assert "pin still overrides" in outcomes[0].describe()


def test_a_pin_naming_nothing_is_reported_rather_than_ignored(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    overrides = pio.Overrides.load(result.directory / pio.OVERRIDES)
    overrides.add_pin("/styles/paragraph/typo/run/size", FontSize.pt(13))
    _, outcomes = pio.apply_overrides(result.consensus, overrides)
    assert outcomes[0].status == "unknown"
    assert "Check the spelling" in outcomes[0].describe()


def test_overrides_with_comments_are_backed_up_before_a_rewrite(tmp_path):
    result = learn(corpus(tmp_path), tmp_path / "prof")
    path = result.directory / pio.OVERRIDES
    path.write_text("# my note about why\nversion: 1\nmuted: ['/x']\n")
    overrides = pio.Overrides.load(path)
    assert overrides.had_comments is True
    notes = overrides.dump(path)
    assert (result.directory / "overrides.yaml.bak").exists()
    assert any("cannot preserve" in n for n in notes)
    assert pio.Overrides.load(path).muted == {"/x"}


def test_unknown_keys_in_overrides_survive_a_rewrite(tmp_path):
    path = tmp_path / pio.OVERRIDES
    path.write_text("version: 1\nfuture_feature: {a: 1}\n")
    overrides = pio.Overrides.load(path)
    overrides.dump(path)
    assert pio.Overrides.load(path).extra == {"future_feature": {"a": 1}}


# -- serialization --------------------------------------------------------


@pytest.mark.parametrize("pointer,value", [
    ("/page/primary/margins/left", Length.inches(1.25)),
    ("/styles/paragraph/x/para/space_after", Length.pt(6)),
    ("/styles/paragraph/x/run/size", FontSize.pt(11.5)),
    ("/x/alignment", "both"),
    ("/x/keep_next", True),
    ("/x/size", None),
])
def test_values_round_trip_through_the_profile(pointer, value):
    assert decode(encode(value, pointer), pointer) == value


def test_a_value_that_cannot_round_trip_is_refused_not_written(tmp_path):
    """Writing one would mean the next run silently disagrees with this one."""
    from formgen.analyze.stats import Vote

    class Weird:
        def __eq__(self, other):
            return False

    consensus = C.Consensus(
        votes={"/x/size": Vote("/x/size", 1, Weird(), ("s", 1), 1, 1)},
        docs=("d",),
    )
    with pytest.raises(ValueError, match="do not survive serialization"):
        pio.write_profile(tmp_path / "p", "p", consensus, pio.Overrides())
