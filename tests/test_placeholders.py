"""Column classification, naming, typing -- and what we refuse to guess."""

from __future__ import annotations

from formgen.learn.align import Item, Section, Skeleton, align
from formgen.learn.placeholders import (
    BOILERPLATE, FREE_CONTENT, NAME_CONFIDENCE, PLACEHOLDER, Slot, _type,
    classify, slug,
)


def skeleton(columns, documents, title=""):
    return Skeleton(
        sections=[Section(heading=None, columns=columns)],
        documents=tuple(documents),
    )


def build(rows: dict[str, list[tuple]], documents=None):
    """rows: doc -> [(kind, key, text, sdt_tag, instruction), ...]"""
    sequences = {}
    for doc, items in rows.items():
        sequences[doc] = [
            Item(kind, key, index, text, tag, instruction)
            for index, (kind, key, text, tag, instruction) in enumerate(items)
        ]
    return skeleton(align(sequences), documents or sorted(rows))


def row(kind, key, text="", tag="", instruction=""):
    return (kind, key, text or key, tag, instruction)


# -- the three kinds -----------------------------------------------------


def test_text_every_document_shares_is_boilerplate():
    profile = classify(build({
        doc: [row("body", "ds", "Distribution Statement A")]
        for doc in "abc"
    }))
    assert [s.kind for s in profile.slots] == [BOILERPLATE]
    assert profile.slots[0].text == "Distribution Statement A"


def test_a_short_value_that_differs_everywhere_is_a_placeholder():
    profile = classify(build({
        doc: [row("body", f"n{doc}", f"Report No. LR-202{i}-0001")]
        for i, doc in enumerate("abc")
    }))
    assert profile.slots[0].kind == PLACEHOLDER
    assert profile.slots[0].split.prefix == "Report No. "


def test_long_differing_prose_is_free_content_not_a_field():
    texts = {
        "a": "The X-7 radiator exceeds its design margin under worst-case loading conditions.",
        "b": "The thermal control system was characterised over a range of operating points.",
        "c": "Margin was computed against the qualification limits given in the specification.",
    }
    profile = classify(build({
        doc: [row("body", doc, text)] for doc, text in texts.items()
    }))
    assert profile.slots[0].kind == FREE_CONTENT


def test_a_column_too_rare_to_matter_is_not_part_of_the_skeleton():
    rows = {doc: [row("body", "shared")] for doc in "abcde"}
    rows["a"].append(row("body", "rare", "Only in one document"))
    profile = classify(build(rows))
    rare = [s for s in profile.slots if s.occupancy < 0.4]
    assert rare and all("too rare" in " ".join(s.notes) for s in rare)


def test_mid_coverage_prose_is_flagged_rather_than_decided():
    """Optional-versus-forgotten is undecidable from a corpus, so it is
    reported as undecided instead of guessed at."""
    rows = {doc: [row("body", "shared")] for doc in "abcd"}
    for doc, text in (("a", "one sort of paragraph entirely unlike the other"),
                      ("b", "a second paragraph that shares nothing with it")):
        rows[doc].append(row("body", f"x{doc}", text))
    profile = classify(build(rows))
    mid = [s for s in profile.slots if 0.4 <= s.occupancy < 0.75]
    assert mid and all(s.needs_review for s in mid)
    assert any("cannot say" in " ".join(s.notes) for s in mid)


# -- naming, in priority order -------------------------------------------


def test_a_content_control_tag_outranks_everything():
    profile = classify(build({
        doc: [row("body", f"n{doc}", f"Report No. LR-{doc}",
                  tag="formgen.report_number")]
        for doc in "abc"
    }))
    slot = profile.placeholders[0]
    assert slot.name == "report_number"
    assert slot.name_confidence == 1.0
    assert not slot.needs_review


def test_a_docproperty_field_names_itself():
    profile = classify(build({
        doc: [row("body", f"n{doc}", f"LR-{doc}",
                  instruction=' DOCPROPERTY "ProjectNumber"  \\* MERGEFORMAT ')]
        for doc in "abc"
    }))
    slot = profile.placeholders[0]
    assert slot.name == "projectnumber"
    assert slot.name_confidence >= 0.9


def test_a_value_matching_a_document_property_takes_that_property_name():
    authors = {"a": "L. Craig", "b": "R. Patel", "c": "K. Ito"}
    profile = classify(
        build({doc: [row("body", f"a{doc}", name)]
               for doc, name in authors.items()}),
        properties={doc: {"Author": name} for doc, name in authors.items()},
    )
    slot = profile.placeholders[0]
    assert slot.name == "author"
    assert "document property" in slot.name_source


def test_a_name_taken_from_a_nearby_label_is_marked_for_review():
    profile = classify(build({
        doc: [row("body", f"n{doc}", f"Report Number: LR-202{i}-0001")]
        for i, doc in enumerate("abc")
    }))
    slot = profile.placeholders[0]
    assert slot.name == "report_number"
    assert slot.name_confidence < NAME_CONFIDENCE
    assert slot.needs_review
    assert slot in profile.unconfident


def test_a_field_with_nothing_to_name_it_falls_back_to_its_position():
    profile = classify(build({
        doc: [row("body", f"v{doc}", value)]
        for doc, value in (("a", "Alpha"), ("b", "Bravo"), ("c", "Charlie"))
    }))
    slot = profile.placeholders[0]
    assert slot.name.startswith("field_")
    assert slot.needs_review


def test_two_columns_wanting_the_same_name_do_not_collide():
    profile = classify(build({
        doc: [row("body", f"p{doc}", f"Number: {i}1"),
              row("body", f"q{doc}", f"Number: {i}2")]
        for i, doc in enumerate("abc")
    }))
    names = [s.name for s in profile.placeholders]
    assert len(set(names)) == len(names)


def test_slug_survives_punctuation():
    assert slug("Report Number / Ref.") == "report_number_ref"
    assert slug("   ") == "field"


# -- type inference ------------------------------------------------------


def typed(values):
    slot = Slot(index=0, kind=PLACEHOLDER, occupancy=1.0, agreement=0.0)
    _type(slot, {str(i): v for i, v in enumerate(values)})
    return slot


def test_dates_are_recognised_across_spellings():
    assert typed(["2024-01-05", "15 March 2026", "3 Jan 2025"]).value_type == "date"


def test_a_measurement_is_not_a_date():
    """dateutil reads "12.4" as the 4th of December, which would type a
    column of measurements as dates for the life of the profile."""
    assert typed(["12.4", "3.1", "0.75"]).value_type == "number"


def test_a_repeated_small_vocabulary_is_an_enum():
    slot = typed(["Draft", "Final", "Draft", "Final", "Draft"])
    assert slot.value_type == "enum"
    assert slot.examples == ("Draft", "Final")


def test_a_shared_shape_becomes_a_pattern():
    slot = typed(["LR-2024-0041", "LR-2026-0142", "LR-2025-0118"])
    assert slot.value_type == "identifier"
    assert slot.pattern == r"^[A-Z]{2}\-\d{4}\-\d{4}$"


def test_values_with_no_shared_shape_get_no_pattern():
    slot = typed(["LR-2024-0041", "an entirely different sort of value"])
    assert slot.pattern == ""


def test_a_pattern_admits_a_length_range_when_the_examples_vary():
    slot = typed(["A-1", "A-22", "A-333"])
    assert slot.pattern == r"^[A-Z]{1}\-\d{1,3}$"


# -- what the rest of the system consumes --------------------------------


def test_overrides_carry_the_type_pattern_and_examples():
    profile = classify(build({
        doc: [row("body", f"n{doc}", f"Report No. LR-202{i}-0001",
                  tag="formgen.report_number")]
        for i, doc in enumerate("abc")
    }))
    entry = profile.as_overrides()["report_number"]
    assert entry["type"] == "identifier"
    assert entry["required"] is True
    assert entry["pattern"].startswith("^")
    assert len(entry["examples"]) == 3


def test_the_json_view_records_the_template_around_a_field():
    profile = classify(build({
        doc: [row("body", f"n{doc}", f"Report No. LR-202{i}-0001",
                  tag="formgen.report_number")]
        for i, doc in enumerate("abc")
    }))
    row_json = profile.as_json()["slots"][0]
    assert row_json["template"] == "Report No. {report_number}"
    assert row_json["kind"] == PLACEHOLDER


def test_a_confident_name_does_not_fail_the_build():
    profile = classify(build({
        doc: [row("body", f"n{doc}", f"LR-{doc}", tag="formgen.report_number")]
        for doc in "abc"
    }))
    assert profile.unconfident == []
    assert profile.warnings == []


def test_the_donor_index_points_at_the_donor_s_own_block():
    profile = classify(
        build({doc: [row("body", "pad", "Shared opening line"),
                     row("body", f"n{doc}", f"Report No. LR-{doc}")]
               for doc in "abc"}),
        donor="b",
    )
    slot = profile.placeholders[0]
    assert slot.donor_index == 1
    assert slot.donor_value == "LR-b"
