"""OPC layer: the preservation guarantee, round-tripping, and integrity checks."""

from __future__ import annotations

import zipfile

import pytest
from lxml import etree

from fixtures.build import make
from formgen.opc.content_types import PART_NAME as CT_PART
from formgen.opc.ns import RT, qn
from formgen.opc.package import OpcPackage, PackageError
from formgen.opc.rels import Relationships, rels_name_for, source_of_rels


def save_reopen(pkg, tmp_path, name="out.docx"):
    path = tmp_path / name
    pkg.save(path, deterministic=True)
    return OpcPackage.open(path)


# -- the guarantee ------------------------------------------------------


def test_untouched_parts_are_byte_identical(tmp_path):
    """Editing one part must not perturb a single byte of any other."""
    pkg = make()
    before = {n: pkg.blob(n) for n in pkg.names()}

    body = pkg.edit(pkg.main_document)
    for p in body.iter(qn("w:p")):
        ppr = p.find(qn("w:pPr"))
        if ppr is not None:
            p.remove(ppr)

    after = save_reopen(pkg, tmp_path)
    for name in before:
        if name == "word/document.xml":
            continue
        assert after.blob(name) == before[name], f"{name} was rewritten"

    assert b"w:pStyle" not in after.blob("word/document.xml")


def test_reading_a_part_does_not_rewrite_it(tmp_path):
    """element() is read-only; only edit() marks a part for reserialization."""
    pkg = make()
    original = pkg.blob("word/styles.xml")

    pkg.element("word/styles.xml")  # parse, but only to read
    assert not pkg.is_dirty("word/styles.xml")

    assert save_reopen(pkg, tmp_path).blob("word/styles.xml") == original

    pkg.edit("word/styles.xml")
    assert pkg.is_dirty("word/styles.xml")


def test_unknown_parts_survive_a_round_trip(tmp_path):
    """Parts we have never heard of are carried through untouched."""
    junk = b"\x00\x01binary payload we do not understand\xff"
    pkg = make()
    pkg.add_part("word/embeddings/oleObject1.bin", junk)
    pkg.add_part("customXml/item1.xml", b"<root><a/></root>")
    pkg.edit(pkg.main_document)  # force a rewrite of something else

    after = save_reopen(pkg, tmp_path)
    assert after.blob("word/embeddings/oleObject1.bin") == junk
    assert after.blob("customXml/item1.xml") == b"<root><a/></root>"


# -- determinism --------------------------------------------------------


def test_deterministic_save_is_byte_reproducible(tmp_path):
    pkg = make()
    a, b = tmp_path / "a.docx", tmp_path / "b.docx"
    pkg.save(a, deterministic=True)
    pkg.save(b, deterministic=True)
    assert a.read_bytes() == b.read_bytes()


def test_content_types_part_is_written_first(tmp_path):
    pkg = make()
    path = tmp_path / "o.docx"
    pkg.save(path, deterministic=True)
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist()[0] == CT_PART


def test_save_is_atomic_and_leaves_no_temp_file(tmp_path):
    pkg = make()
    path = tmp_path / "o.docx"
    pkg.save(path)
    assert path.exists()
    assert list(tmp_path.glob("*.formgen-tmp")) == []


# -- part mutation ------------------------------------------------------


def test_add_part_registers_a_content_type(tmp_path):
    pkg = make()
    pkg.add_part("word/media/image9.png", b"\x89PNG\r\n")
    assert pkg.content_types.for_part("word/media/image9.png") == "image/png"
    assert save_reopen(pkg, tmp_path).missing_content_types() == []


def test_add_part_with_unknown_extension_is_reported_by_validation():
    pkg = make()
    pkg.add_part("word/weird.zzz", b"x")
    assert "word/weird.zzz" in pkg.missing_content_types()


def test_drop_part_removes_its_rels_and_override(tmp_path):
    pkg = make()
    pkg.add_part("word/header1.xml", b"<x/>", "ct/header")
    pkg.touch_rels("word/header1.xml").add(RT["image"], "media/logo.png")
    assert rels_name_for("word/header1.xml") in pkg.names()

    pkg.drop_part("word/header1.xml")
    assert "word/header1.xml" not in pkg
    assert rels_name_for("word/header1.xml") not in pkg.names()
    # The Override is gone. for_part still resolves via Default Extension="xml",
    # which is correct OPC behaviour -- every .xml part has a fallback type.
    assert "word/header1.xml" not in pkg.content_types.overrides


def test_unique_partname_is_case_insensitive():
    """Minting Image1.png beside image1.png is legal in zip but breaks Windows."""
    pkg = make(extra_parts={"word/media/image1.png": b"x"})
    assert pkg.unique_partname("word/media/Image{n}.png") == "word/media/Image2.png"


def test_add_existing_part_is_rejected():
    pkg = make()
    with pytest.raises(PackageError):
        pkg.add_part("word/styles.xml", b"<x/>")


# -- relationships ------------------------------------------------------


def test_well_known_parts_resolve_through_relationships():
    pkg = make()
    assert pkg.main_document == "word/document.xml"
    assert pkg.related(RT["styles"]) == "word/styles.xml"
    assert pkg.related(RT["numbering"]) == "word/numbering.xml"
    assert pkg.related(RT["theme"]) == "word/theme/theme1.xml"
    assert pkg.related(RT["header"]) is None


def test_rel_targets_resolve_relative_to_the_source_part():
    rels = Relationships.parse(
        "word/document.xml",
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="T" Target="../customXml/item1.xml"/>'
        b'<Relationship Id="rId2" Type="T" Target="/word/abs.xml"/>'
        b'<Relationship Id="rId3" Type="T" Target="http://x" TargetMode="External"/>'
        b"</Relationships>",
    )
    assert rels.target_of("rId1") == "customXml/item1.xml"
    assert rels.target_of("rId2") == "word/abs.xml"
    assert rels.target_of("rId3") is None


def test_rid_allocation_fills_gaps_and_never_collides():
    rels = Relationships("word/document.xml")
    rels.add("T", "a.xml")           # rId1
    rels.add("T", "b.xml")           # rId2
    rels.drop("rId1")
    assert rels.next_rid() == "rId1"
    assert rels.add("T", "c.xml") == "rId1"
    assert rels.add("T", "d.xml") == "rId3"


def test_rels_name_round_trips():
    for partname in ["word/document.xml", "", "word/header1.xml", "docProps/core.xml"]:
        assert source_of_rels(rels_name_for(partname)) == partname


def test_added_rels_survive_a_round_trip(tmp_path):
    pkg = make()
    rid = pkg.touch_rels(pkg.main_document).add(RT["hyperlink"], "http://e.com", external=True)

    after = save_reopen(pkg, tmp_path)
    rel = after.rels(after.main_document).get(rid)
    assert rel is not None and rel.external and rel.target == "http://e.com"
    # the pre-existing relationships must still be there
    assert after.related(RT["styles"]) == "word/styles.xml"


# -- integrity ----------------------------------------------------------


def test_dangling_rel_is_detected():
    pkg = make()
    pkg.touch_rels(pkg.main_document).add(RT["header"], "header1.xml")
    assert ("word/document.xml", "rId5", "header1.xml") in pkg.dangling_rels()


def test_clean_fixture_has_no_integrity_faults():
    pkg = make()
    assert pkg.dangling_rels() == []
    assert pkg.missing_content_types() == []


def test_external_rels_are_not_dangling():
    pkg = make()
    pkg.touch_rels(pkg.main_document).add(RT["hyperlink"], "http://e.com", external=True)
    assert pkg.dangling_rels() == []


def test_sha256_is_stable_and_content_sensitive():
    a, b = make(), make()
    assert a.sha256() == b.sha256()
    c = make(title="Different")
    assert c.sha256() != a.sha256()


def test_clone_is_independent():
    pkg = make()
    clone = pkg.clone()
    body = clone.edit(clone.main_document)
    body.find(qn("w:body")).append(etree.SubElement(body, qn("w:p")))
    assert clone.blob("word/document.xml") != pkg.blob("word/document.xml")


# -- failure modes ------------------------------------------------------


def test_non_zip_input_gets_an_actionable_error(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")  # OLE2 magic
    with pytest.raises(PackageError, match="OLE2 compound file"):
        OpcPackage.open(path)


def test_encrypted_document_is_named_as_such(tmp_path):
    """A .doc and an encrypted .docx are both CFB; the stream names differ."""
    path = tmp_path / "locked.docx"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                     + b"\x00" * 64 + "EncryptedPackage".encode("utf-16-le"))
    with pytest.raises(PackageError, match="encrypted/password-protected"):
        OpcPackage.open(path)


def test_word97_binary_doc_is_named_as_such(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                     + b"\x00" * 64 + "WordDocument".encode("utf-16-le"))
    with pytest.raises(PackageError, match="Word 97-2003"):
        OpcPackage.open(path)


def test_flat_opc_is_named_as_such(tmp_path):
    path = tmp_path / "flat.xml"
    path.write_bytes(b'<?xml version="1.0"?><pkg:package xmlns:pkg="x"/>')
    with pytest.raises(PackageError, match="Flat OPC"):
        OpcPackage.open(path)


def test_package_without_content_types_is_rejected():
    with pytest.raises(PackageError, match="Content_Types"):
        OpcPackage({"word/document.xml": b"<x/>"})


def test_package_without_office_document_rel_is_rejected():
    pkg = make()
    pkg.replace_part(
        "_rels/.rels",
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
    )
    with pytest.raises(PackageError, match="officeDocument"):
        _ = pkg.main_document


def test_malformed_xml_names_the_part():
    pkg = make(extra_parts={"word/broken.xml": b"<w:p><unclosed>"})
    with pytest.raises(PackageError, match="word/broken.xml"):
        pkg.element("word/broken.xml")


def test_missing_part_raises():
    pkg = make()
    with pytest.raises(PackageError, match="no such part"):
        pkg.blob("word/nope.xml")


def test_prefix_derivable_part_name_is_rejected():
    """OPC M1.11: such a pair cannot be extracted to a filesystem."""
    pkg = make()
    pkg.add_part("word/media/logo.png", b"\x89PNG")
    with pytest.raises(PackageError, match="derivable"):
        pkg.add_part("word/media/logo.png/thumb.png", b"x")


def test_duplicate_zip_entries_are_rejected_not_silently_dropped(tmp_path):
    """Last-writer-wins would discard a part while promising byte-faithfulness."""
    path = tmp_path / "dup.docx"
    src = make()
    with zipfile.ZipFile(path, "w") as zf:
        for name in src.names():
            zf.writestr(name, src.blob(name))
        zf.writestr("word/styles.xml", b"<second/>")
    with pytest.raises(PackageError, match="more than one entry"):
        OpcPackage.open(path)


def test_part_names_compare_case_insensitively():
    pkg = make()
    assert "WORD/STYLES.XML" in pkg
    assert pkg.actual_name("Word/Styles.XML") == "word/styles.xml"


def test_leading_slash_entry_names_are_normalized():
    src = make()
    blobs = {("/" + n if n == "word/document.xml" else n): src.blob(n)
             for n in src.names()}
    pkg = OpcPackage(blobs)
    assert pkg.main_document == "word/document.xml"
    assert pkg.element(pkg.main_document) is not None


def test_save_refuses_a_package_word_would_reject(tmp_path):
    pkg = make()
    pkg.touch_rels(pkg.main_document).add(RT["header"], "header1.xml")
    with pytest.raises(PackageError, match="missing target"):
        pkg.save(tmp_path / "broken.docx")


def test_relationship_edits_survive_even_after_the_rels_part_is_inspected(tmp_path):
    """Rels parts are a closed namespace; both caches cannot disagree."""
    pkg = make()
    rid = pkg.touch_rels(pkg.main_document).add(
        RT["hyperlink"], "http://e.com", external=True)
    with pytest.raises(PackageError, match="relationship part"):
        pkg.element(rels_name_for(pkg.main_document))
    assert rid.encode() in pkg.blob(rels_name_for(pkg.main_document))


def test_dropping_a_part_also_removes_relationships_pointing_at_it():
    pkg = make()
    pkg.add_part("word/header1.xml", b"<x/>", "ct/header")
    pkg.relate(RT["header"], "word/header1.xml", pkg.main_document)
    pkg.drop_part("word/header1.xml")
    assert pkg.dangling_rels() == []


def test_duplicate_relationship_ids_are_rejected():
    from formgen.opc.errors import TargetError

    blob = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="T" Target="a.xml"/>'
            b'<Relationship Id="rId1" Type="T" Target="b.xml"/></Relationships>')
    with pytest.raises(TargetError, match="duplicate"):
        Relationships.parse("word/document.xml", blob)


def test_non_relationship_children_are_not_reserialized_as_relationships():
    blob = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="T" Target="a.xml"/>'
            b'<NotARelationship Id="rId3"/></Relationships>')
    rels = Relationships.parse("word/document.xml", blob)
    assert [r.rid for r in rels] == ["rId1"]


def test_external_target_mode_is_matched_case_insensitively():
    blob = (b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rId1" Type="T" Target="http://e.com" TargetMode="external"/>'
            b"</Relationships>")
    assert Relationships.parse("word/document.xml", blob).get("rId1").external


def test_percent_escapes_in_targets_are_not_decoded():
    """OPC equivalence is over the literal encoded string; decoding invents
    dangling relationships for every image with a space in its name."""
    from formgen.opc.rels import resolve_target

    assert resolve_target("word/document.xml", "media/image%201.png") == (
        "word/media/image%201.png")


def test_dot_segments_are_clamped_at_the_package_root():
    from formgen.opc.rels import resolve_target

    assert resolve_target("word/document.xml", "../../../x.xml") == "x.xml"


def test_serialized_bytes_are_cached_between_calls():
    pkg = make()
    pkg.edit(pkg.main_document)
    assert pkg.blob("word/document.xml") is pkg.blob("word/document.xml")


def test_dropping_the_last_relationship_of_a_part_reaches_the_saved_file(tmp_path):
    """`cached or parse(blob)` re-read the original bytes when the cache was EMPTY.

    Relationships defines __len__, so an emptied collection is falsy. Dropping
    a part that something referenced left the in-memory rels correct and the
    integrity check reading the stale blob -- so save refused to write a
    package that was, in fact, fine. Found on a real document whose footer
    held a logo.
    """
    from fixtures import build

    pkg = build.make()
    build.add_hdrftr(pkg, "footer", "footer1.xml", "Acme")
    pkg.add_part("word/media/logo.png", b"\x89PNG\r\n\x1a\nLOGO", "image/png")
    pkg.relate(RT["image"], "word/media/logo.png", "word/footer1.xml")
    assert pkg.dangling_rels() == []

    pkg.drop_part("word/media/logo.png")
    assert [r.rid for r in pkg.rels("word/footer1.xml")] == []
    assert pkg.dangling_rels() == [], "the emptied rels were read from the stale blob"

    out = tmp_path / "out.docx"
    pkg.save(out, deterministic=True)
    reopened = OpcPackage.open(out)
    assert "word/media/logo.png" not in reopened
    assert [r.rid for r in reopened.rels("word/footer1.xml")] == []
