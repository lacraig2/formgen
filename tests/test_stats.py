"""Consensus voting: coverage and agreement are two numbers, never one."""

from __future__ import annotations

import pytest

from formgen.analyze.stats import (
    Vote,
    bucket,
    disagreement,
    profile_distance,
    vote_all,
    vote_one,
)
from formgen.oox.values import FontSize, Length, LineSpacing

DOCS = [f"d{i}" for i in range(1, 13)]


def obs(**per_doc):
    """{'d1': 11, 'd2': [11, 12]} -> [(doc, value), ...]"""
    out = []
    for doc, values in per_doc.items():
        if not isinstance(values, list):
            values = [values]
        out.extend((doc, v) for v in values)
    return out


# -- the distinction the module exists for --------------------------------


def test_rare_but_consistent_is_not_contested():
    """3 of 12 documents, all agreeing: low coverage, perfect agreement."""
    v = vote_one("/styles/sidebar/size", obs(d1=1, d2=1, d3=1), 12, DOCS)
    assert v.coverage == pytest.approx(0.25)
    assert v.agreement == 1.0
    assert v.status == "informational"
    assert v.severity == "off"


def test_common_and_unanimous_is_normative():
    v = vote_one("/styles/body/size", obs(**{d: 22 for d in DOCS}), 12, DOCS)
    assert v.status == "normative"
    assert v.severity == "error"
    assert v.in_donor is True
    assert v.needs_review is False


def test_common_with_a_minority_is_contested_not_undecided():
    values = {d: 22 for d in DOCS}
    values["d11"] = values["d12"] = 24
    v = vote_one("/styles/body/size", obs(**values), 12, DOCS)
    assert v.agreement == pytest.approx(10 / 12)
    assert v.status == "contested"
    assert v.severity == "warn"
    assert v.needs_review is True
    assert v.dissenting == ("d11", "d12")


def test_no_majority_is_undecided_and_lints_nothing():
    values = {d: 20 + i for i, d in enumerate(DOCS)}
    v = vote_one("/styles/body/size", obs(**values), 12, DOCS)
    assert v.agreement < 0.5
    assert v.status == "undecided"
    assert v.severity == "off"
    assert v.in_donor is True     # the donor still needs *a* value


def test_half_the_corpus_agreeing_perfectly_is_optional_consistent():
    v = vote_one("/styles/annex/size", obs(**{d: 20 for d in DOCS[:6]}), 12, DOCS)
    assert v.coverage == 0.5
    assert v.status == "optional_consistent"
    assert v.severity == "error-if-present"


def test_half_the_corpus_disagreeing_is_merely_variable():
    values = {d: 20 for d in DOCS[:4]}
    values.update({d: 24 for d in DOCS[4:6]})
    v = vote_one("/styles/annex/size", obs(**values), 12, DOCS)
    assert v.status == "variable"
    assert v.severity == "info"


def test_silent_documents_are_named():
    v = vote_one("/x", obs(d1=1, d2=1), 12, DOCS)
    assert set(v.silent) == set(DOCS) - {"d1", "d2"}


# -- one document, one vote ----------------------------------------------


def test_a_long_document_does_not_outvote_a_short_one():
    """300 paragraphs in one exemplar must not decide the house format."""
    v = vote_one(
        "/styles/body/space_after",
        obs(d1=[Length(120)] * 300, d2=[Length(240)], d3=[Length(240)]),
        3,
        ["d1", "d2", "d3"],
    )
    assert v.value == Length(240)
    assert v.modal_count == 2


def test_a_documents_internal_disagreement_is_recorded_not_discarded():
    """Fighting the styles with direct formatting is a donor-quality signal."""
    v = vote_one(
        "/styles/body/size",
        obs(d1=[FontSize(22)] * 8 + [FontSize(24)] * 2, d2=[FontSize(22)]),
        2,
        ["d1", "d2"],
    )
    messy, clean = v.per_doc
    assert messy.internal_agreement == pytest.approx(0.8)
    assert clean.internal_agreement == 1.0
    assert v.value == FontSize(22)


# -- quantization ---------------------------------------------------------


def test_font_sizes_within_half_a_point_share_a_bucket():
    assert bucket(FontSize.pt(11.0), "size") == bucket(FontSize.pt(11.2), "size")
    assert bucket(FontSize.pt(11.0), "size") != bucket(FontSize.pt(11.5), "size")


def test_indents_bucket_at_a_hundredth_of_an_inch():
    a = bucket(Length.inches(0.5), "indent_left")
    assert a == bucket(Length(Length.inches(0.5).twips + 3), "indent_left")
    assert a != bucket(Length.inches(0.51), "indent_left")


def test_spacing_buckets_at_a_point():
    assert bucket(Length.pt(6), "space_after") == bucket(Length(121), "space_after")
    assert bucket(Length.pt(6), "space_after") != bucket(Length.pt(7), "space_after")


def test_line_spacing_never_compares_across_rules():
    """240 twentieths is single spacing at auto and 12pt at exact."""
    auto = LineSpacing("auto", 240)
    exact = LineSpacing("exact", 240)
    assert bucket(auto) != bucket(exact)


def test_a_length_can_never_collide_with_a_font_size():
    assert bucket(Length(240), "x") != bucket(FontSize(240), "x")


def test_near_identical_values_vote_together_but_a_real_value_is_published():
    """The donor gets a number an author chose, not a bucket centre."""
    v = vote_one(
        "/page/margins/left",
        obs(d1=Length(1440), d2=Length(1443), d3=Length(1438)),
        3,
        ["d1", "d2", "d3"],
    )
    assert v.agreement == 1.0
    assert v.value in (Length(1440), Length(1443), Length(1438))
    assert v.value == Length(1440)      # median_low of the three


# -- determinism ----------------------------------------------------------


def test_a_tie_resolves_the_same_way_whatever_the_input_order():
    forward = vote_one("/x", obs(d1="a", d2="b"), 2, ["d1", "d2"])
    reverse = vote_one("/x", obs(d2="b", d1="a"), 2, ["d2", "d1"])
    assert forward.modal_bucket == reverse.modal_bucket
    assert forward.value == reverse.value


def test_two_exemplars_that_agree_are_flagged_as_weak():
    v = vote_one("/x", obs(d1=1, d2=1), 2, ["d1", "d2"])
    assert v.agreement == 1.0
    assert v.weak is True
    assert "weak" in v.explain()


# -- alternatives and reporting ------------------------------------------


def test_alternatives_are_ordered_by_support():
    values = {d: 22 for d in DOCS[:7]}
    values.update({d: 24 for d in DOCS[7:10]})
    values.update({d: 20 for d in DOCS[10:]})
    v = vote_one("/styles/body/size", obs(**values), 12, DOCS)
    assert [count for _, count, _ in v.alternatives] == [3, 2]
    assert [raw for _, _, raw in v.alternatives] == [24, 20]


def test_explain_reads_as_a_sentence():
    v = vote_one("/styles/body/size", obs(**{d: 22 for d in DOCS}), 12, DOCS)
    text = v.explain()
    assert "normative" in text and "100% coverage" in text and "12/12" in text


# -- corpus-level helpers -------------------------------------------------


def test_vote_all_keys_are_sorted_and_complete():
    votes = vote_all(
        {"/b": obs(d1=1, d2=1), "/a": obs(d1=2)}, ["d1", "d2"]
    )
    assert list(votes) == ["/a", "/b"]
    assert votes["/a"].coverage == 0.5


def test_disagreement_lists_only_shared_keys_that_differ():
    a = vote_all({"/x": obs(d1=1), "/y": obs(d1=1)}, ["d1"])
    b = vote_all({"/x": obs(d2=2), "/z": obs(d2=1)}, ["d2"])
    assert disagreement(a, b) == ["/x"]


def test_profile_distance_ignores_properties_only_one_document_has():
    a = {"/size": FontSize(22), "/jc": "left", "/only_a": 1}
    b = {"/size": FontSize(22), "/jc": "both"}
    assert profile_distance(a, b) == pytest.approx(0.5)
    assert profile_distance({}, {}) == 1.0


def test_profile_distance_uses_the_same_buckets_as_the_vote():
    a = {"/indent_left": Length.inches(0.5)}
    b = {"/indent_left": Length(Length.inches(0.5).twips + 2)}
    assert profile_distance(a, b) == 0.0
