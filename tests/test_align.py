"""Alignment: the part that decides what the skeleton even is."""

from __future__ import annotations

from formgen.learn.align import (
    MAX_GAP, Item, align, build_skeleton, medoid, similarity,
    split_variable_part,
)


def seq(*pairs):
    return [Item(kind, key, index, text or key)
            for index, (kind, key, *rest) in enumerate(pairs)
            for text in [rest[0] if rest else key]]


# -- the basics ----------------------------------------------------------


def test_identical_documents_align_to_one_column_each():
    one = seq(("h1", "intro"), ("body", "a"), ("h1", "methods"))
    columns = align({"a": list(one), "b": list(one), "c": list(one)})
    assert len(columns) == 3
    assert all(len(column) == 3 for column in columns)


def test_a_block_only_one_document_has_gets_its_own_column():
    columns = align({
        "a": seq(("h1", "intro"), ("h1", "methods")),
        "b": seq(("h1", "intro"), ("h1", "background"), ("h1", "methods")),
    })
    occupancy = [sorted(column.members) for column in columns]
    assert occupancy == [["a", "b"], ["b"], ["a", "b"]]


def test_differing_text_in_the_same_slot_lands_in_one_column():
    """The whole point. A placeholder is a column whose members disagree, so
    an aligner that only matches identical text can never find one."""
    columns = align({
        doc: seq(("h1", "cover"), ("body", f"num-{doc}", f"Report No. {doc}"),
                 ("h1", "intro"))
        for doc in ("a", "b", "c")
    })
    assert len(columns) == 3
    assert sorted(columns[1].members) == ["a", "b", "c"]
    assert columns[1].agreement() < 1.0


def test_an_item_compares_on_kind_and_key_only():
    assert Item("body", "x", 1, "one") == Item("body", "x", 99, "two")
    assert Item("body", "x") != Item("h1", "x")
    assert len({Item("body", "x", 1), Item("body", "x", 2)}) == 1


# -- determinism ---------------------------------------------------------


def test_the_result_does_not_depend_on_the_order_of_the_corpus():
    docs = {
        "a": seq(("h1", "intro"), ("body", "p"), ("h1", "methods")),
        "b": seq(("h1", "intro"), ("h1", "methods")),
        "c": seq(("h1", "intro"), ("body", "p"), ("h1", "results")),
    }
    first = align(docs)
    second = align({k: docs[k] for k in reversed(list(docs))})
    assert [(sorted(c.members), c.modal_token()) for c in first] == \
           [(sorted(c.members), c.modal_token()) for c in second]


def test_the_medoid_breaks_ties_on_the_document_id():
    docs = {"b": seq(("h1", "x")), "a": seq(("h1", "x"))}
    assert medoid(docs) == "a"


def test_similarity_is_one_for_two_empty_sequences():
    assert similarity([], []) == 1.0


# -- the two tiers -------------------------------------------------------


def test_a_section_body_is_aligned_within_its_own_section():
    headings = {doc: seq(("h1", "intro"), ("h1", "methods"))
                for doc in ("a", "b")}
    bodies = {
        "a": {-1: seq(("body", "cover")), 0: seq(("body", "p1")), 1: seq(("body", "p2"))},
        "b": {-1: seq(("body", "cover")), 0: seq(("body", "p1")), 1: seq(("body", "p9"))},
    }
    skeleton = build_skeleton(headings, bodies)
    assert [section.title for section in skeleton.sections] == \
        ["", "intro", "methods"]
    assert len(skeleton.columns) == 5


def test_a_section_too_long_to_align_is_declared_free_rather_than_forced():
    headings = {doc: seq(("h1", "intro")) for doc in ("a", "b")}
    long_body = [Item("body", f"k{i}", i, f"paragraph {i}")
                 for i in range(MAX_GAP + 5)]
    bodies = {"a": {-1: [], 0: long_body}, "b": {-1: [], 0: list(long_body)}}
    skeleton = build_skeleton(headings, bodies)
    section = skeleton.sections[-1]
    assert section.free and section.columns == []
    assert any("free content" in w for w in skeleton.warnings)


def test_everything_before_the_first_heading_is_its_own_section():
    """That is the cover page, and the cover page is where the fields live."""
    headings = {"a": seq(("h1", "intro")), "b": seq(("h1", "intro"))}
    bodies = {doc: {-1: seq(("body", "cover")), 0: []} for doc in ("a", "b")}
    skeleton = build_skeleton(headings, bodies)
    assert skeleton.sections[0].heading is None
    assert len(skeleton.sections[0].columns) == 1


def test_required_headings_are_those_most_documents_have():
    headings = {
        "a": seq(("h1", "intro"), ("h1", "methods")),
        "b": seq(("h1", "intro"), ("h1", "methods")),
        "c": seq(("h1", "intro"), ("h1", "extra"), ("h1", "methods")),
        "d": seq(("h1", "intro"), ("h1", "methods")),
    }
    bodies = {doc: {} for doc in headings}
    skeleton = build_skeleton(headings, bodies)
    required = [c.modal_token().key for c in skeleton.required_headings()]
    assert required == ["intro", "methods"]


# -- splitting a paragraph -----------------------------------------------


def test_a_shared_prefix_is_split_from_the_varying_value():
    split = split_variable_part({
        "a": "Report No. LR-2024-0041",
        "b": "Report No. LR-2025-0118",
    })
    assert split.prefix == "Report No. "
    assert split.suffix == ""
    assert split.values == {"a": "LR-2024-0041", "b": "LR-2025-0118"}
    assert split.is_field
    assert split.template("report_number") == "Report No. {report_number}"


def test_a_suffix_is_split_too():
    split = split_variable_part({
        "a": "Prepared by L. Craig for the Sponsor",
        "b": "Prepared by R. Patel for the Sponsor",
    })
    assert split.prefix == "Prepared by "
    assert split.suffix == " for the Sponsor"


def test_identical_text_is_not_a_field():
    split = split_variable_part({"a": "Distribution A", "b": "Distribution A"})
    assert split is None or not split.is_field


def test_wholly_different_text_has_no_frame_to_find():
    assert split_variable_part({"a": "one two", "b": "three four"}) is None


def test_one_document_cannot_establish_a_frame():
    assert split_variable_part({"a": "Report No. LR-1"}) is None
