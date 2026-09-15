"""JSON Pointers: the key space every learned value is addressed by."""

from __future__ import annotations

import pytest

from formgen.util.pointer import (
    assign, escape, flatten, leaf, nest, ptr, resolve, split, under, unescape,
)


def test_a_style_name_containing_a_slash_stays_one_token():
    """"Heading 1/2" is a real style in engineering templates; an unescaped
    slash would silently split it into two levels of nesting."""
    pointer = ptr("styles", "paragraph", "heading 1/2", "run", "size")
    assert pointer == "/styles/paragraph/heading 1~12/run/size"
    assert split(pointer)[2] == "heading 1/2"


def test_tilde_escapes_before_slash_so_the_pair_round_trips():
    assert unescape(escape("a~1b")) == "a~1b"
    assert escape("a~1b") == "a~01b"


def test_leaf_is_what_quantization_keys_on():
    assert leaf("/page/primary/margins/left") == "left"
    assert leaf("") == ""


def test_nest_and_flatten_are_inverses():
    flat = {
        "/styles/paragraph/body text/run/size": "11pt",
        "/styles/paragraph/body text/para/alignment": "both",
        "/page/primary/margins/left": "1in",
    }
    assert flatten(nest(flat)) == flat


def test_nest_builds_the_document_a_human_reads():
    assert nest({"/a/b": 1, "/a/c": 2}) == {"a": {"b": 1, "c": 2}}


def test_resolve_returns_the_default_rather_than_raising():
    document = {"a": {"b": [10, 20]}}
    assert resolve(document, "/a/b/1") == 20
    assert resolve(document, "/a/zz", "fallback") == "fallback"
    assert resolve(document, "/a/b/9", None) is None


def test_assign_creates_intermediate_levels():
    document: dict = {}
    assign(document, "/a/b/c", 1)
    assert document == {"a": {"b": {"c": 1}}}


def test_under_matches_whole_tokens_only():
    """"/styles/heading 1" must not match "/styles/heading 10"."""
    pointers = [
        "/styles/heading 1", "/styles/heading 1/run/size", "/styles/heading 10",
    ]
    assert under(pointers, "/styles/heading 1") == [
        "/styles/heading 1", "/styles/heading 1/run/size",
    ]


def test_a_malformed_pointer_is_rejected():
    with pytest.raises(ValueError, match="not a JSON Pointer"):
        split("styles/x")
