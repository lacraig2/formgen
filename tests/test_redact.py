"""The template ships a format, not the document it was learned from.

A donor is somebody's real report. `scrub` takes their name off it; these
tests are about taking their report out of it -- and, just as importantly,
about not taking the format with it. Every test that asserts something was
removed is paired with one asserting something was kept, because a redaction
that empties the template is not safer than one that leaks, it is just broken
in the other direction.
"""

from __future__ import annotations

from lxml import etree

from fixtures import build
from formgen.cli import cli
from formgen.learn import redact as R
from formgen.learn.pipeline import learn
from formgen.opc.ns import RT, qn
from formgen.opc.package import OpcPackage
from formgen.profile import io as pio
from formgen.safety.verify import check_integrity

CUSTOM_CT = ("application/vnd.openxmlformats-officedocument."
             "custom-properties+xml")
APP_CT = ("application/vnd.openxmlformats-officedocument."
          "extended-properties+xml")
COMMENTS_CT = ("application/vnd.openxmlformats-officedocument."
               "wordprocessingml.comments+xml")
FOOTNOTES_CT = ("application/vnd.openxmlformats-officedocument."
                "wordprocessingml.footnotes+xml")

# Each exemplar says something different here, so the corpus cannot agree and
# the text is the donor's alone.
PROSE = {
    0: "The panel was soaked at 340 K for six hours and the margin held.",
    1: "Vibration ran to nine axes and the fixture resonated near mode two.",
    2: "Contamination control was waived once the customer accepted the risk.",
    3: "Acoustic testing was deferred to the following quarter by agreement.",
}
BOILERPLATE = "DISTRIBUTION STATEMENT A. Approved for public release."


def exemplar(n: int, *, creator: str = "formgen tests",
             footer: str | None = None, extra_body: str = "") -> OpcPackage:
    body = (
        build.para("Thermal Margin Analysis", style="Title")
        + build.para(f"Report No. LR-2024-004{n}", style="BodyText")
        + build.para(BOILERPLATE, style="BodyText")
        + build.para("Introduction", style="Heading1")
        + build.para(PROSE[n], style="BodyText")
        + extra_body
    )
    pkg = build.make(body, creator=creator)
    if footer is not None:
        rid = build.add_hdrftr(pkg, "footer", "footer1.xml", footer)
        # Into the section the document already has: appending a paragraph
        # after the body-level w:sectPr is exactly the corruption the
        # integrity check exists to catch.
        sect = pkg.edit(pkg.main_document).find(f"{qn('w:body')}/{qn('w:sectPr')}")
        sect.insert(0, etree.fromstring(
            f'<w:footerReference {build.W} {build.R} w:type="default" '
            f'r:id="{rid}"/>'))
    return pkg


def corpus(tmp_path, n=3, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        path = tmp_path / f"report{i}.docx"
        exemplar(i, **kwargs).save(path, deterministic=True)
        paths.append(path)
    return paths


def learned(tmp_path, n=3, **kwargs):
    paths = corpus(tmp_path / "corpus", n=n, **kwargs)
    result = learn(paths, tmp_path / "profile")
    return result, OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)


def text_of(pkg: OpcPackage, part: str | None = None) -> str:
    part = part or pkg.main_document
    return " ".join(pkg.element(part).itertext())


# -- the body: agreement is what tells format from data -------------------


def test_the_donors_own_prose_does_not_reach_the_template(tmp_path):
    _, template = learned(tmp_path)
    body = text_of(template)
    for prose in list(PROSE.values())[:3]:
        assert prose not in body, "the donor's own sentence shipped"


def test_but_boilerplate_and_headings_survive(tmp_path):
    """The other half of the rule, and the one that makes it usable.

    A redaction that removed the distribution statement and the section
    headings would leave a template nobody could write a report from.
    """
    _, template = learned(tmp_path)
    body = text_of(template)
    assert BOILERPLATE in body
    assert "Introduction" in body


def test_a_placeholders_frame_survives_but_its_value_does_not(tmp_path):
    _, template = learned(tmp_path)
    body = text_of(template)
    assert "Report No." in body
    assert "LR-2024-0040" not in body


def test_below_three_exemplars_the_body_is_kept_and_the_user_is_told(tmp_path):
    """No corpus, no agreement, no basis for deciding -- so say so.

    Guessing here would either ship the report or gut the template, and both
    are worse than a sentence telling the user to look.
    """
    result, template = learned(tmp_path, n=2)
    assert PROSE[0] in text_of(template)
    assert any("fewer than three exemplars" in note for note in result.notes)


def test_consecutive_cleared_paragraphs_collapse_to_one(tmp_path):
    paths = []
    (tmp_path / "corpus").mkdir(parents=True)
    for i in range(3):
        body_extra = "".join(
            build.para(f"Filler {i}-{j} unique to this document entirely.",
                       style="BodyText") for j in range(6)
        )
        path = tmp_path / "corpus" / f"r{i}.docx"
        exemplar(i, extra_body=body_extra).save(path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    body = template.element(template.main_document).find(qn("w:body"))
    empties = [p for p in body.findall(qn("w:p"))
               if not "".join(p.itertext()).strip()]
    assert len(empties) <= 2, "six cleared paragraphs left six empty ones"


def test_a_paragraph_carrying_a_section_break_is_never_removed(tmp_path):
    """w:sectPr rides in the last paragraph of its section.

    Collapsing that paragraph away takes the page setup with it, which is the
    one thing a format donor exists to carry.
    """
    _, template = learned(tmp_path, footer="Lab  |  Confidential")
    body = template.element(template.main_document).find(qn("w:body"))
    found = body.findall(f".//{qn('w:sectPr')}")
    assert found, "the section break was collapsed away with the text"


def test_a_table_cell_is_never_emptied_of_every_paragraph(tmp_path):
    """Word repairs a w:tc with no w:p, which is the prompt we exist to avoid."""
    (tmp_path / "corpus").mkdir(parents=True)
    paths = []
    for i in range(3):
        cells = build.table(
            build.cell(build.para("Report No.")) + build.cell(build.para(f"LR-{i}")),
            build.cell(build.para(f"Author note {i} which varies per document"))
            + build.cell(build.para(f"Value {i}")),
        )
        path = tmp_path / "corpus" / f"r{i}.docx"
        exemplar(i, extra_body=cells).save(path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    for cell in template.element(template.main_document).iter(qn("w:tc")):
        assert cell.findall(qn("w:p")), "a table cell was left with no paragraph"


# -- parts that are pure payload -----------------------------------------


def with_extra_parts(pkg: OpcPackage) -> OpcPackage:
    pkg.add_part("docProps/thumbnail.jpeg", b"\xff\xd8SECRETPAGEONE\xff\xd9",
                 "image/jpeg")
    pkg.relate(RT["thumbnail"], "docProps/thumbnail.jpeg", "")
    pkg.add_part("customXml/item1.xml", b"<root><client>ACME</client></root>",
                 "application/xml")
    pkg.relate(RT["customXml"], "customXml/item1.xml", pkg.main_document)
    pkg.add_part("docProps/custom.xml", (
        '<?xml version="1.0"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
        '2006/custom-properties" xmlns:vt="http://schemas.openxmlformats.org/'
        'officeDocument/2006/docPropsVTypes">'
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" '
        'name="ProjectNumber"><vt:lpwstr>P-99812</vt:lpwstr></property>'
        '</Properties>').encode(), CUSTOM_CT)
    pkg.relate(RT["custom"], "docProps/custom.xml", "")
    pkg.add_part("docProps/app.xml", (
        '<?xml version="1.0"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
        '2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/'
        'officeDocument/2006/docPropsVTypes">'
        '<Company>Acme Defense</Company>'
        '<TitlesOfParts><vt:vector size="1" baseType="lpstr">'
        '<vt:lpstr>Thermal Margin Analysis of the X-7</vt:lpstr>'
        '</vt:vector></TitlesOfParts></Properties>').encode(), APP_CT)
    pkg.relate(RT["extended"], "docProps/app.xml", "")
    return pkg


def test_the_page_one_thumbnail_is_dropped():
    """The leak nobody inspects for: a JPEG of the cover, shown in Explorer."""
    pkg = with_extra_parts(build.make())
    report = R.redact(pkg)
    assert "docProps/thumbnail.jpeg" not in pkg
    assert "docProps/thumbnail.jpeg" in report.dropped_parts


def test_the_custom_xml_store_is_dropped():
    """Scrub unbinds the controls; the data they were bound to still sat here."""
    pkg = with_extra_parts(build.make())
    R.redact(pkg)
    assert "customXml/item1.xml" not in pkg
    assert not [n for n in pkg.names() if n.startswith("customXml/")]


def test_custom_properties_keep_their_names_and_lose_their_values():
    """A DOCPROPERTY field in boilerplate refers to a property by name.

    Deleting the property would leave the field unresolvable, so the name is
    format and only the value is data.
    """
    pkg = with_extra_parts(build.make())
    R.redact(pkg)
    xml = pkg.blob("docProps/custom.xml").decode()
    assert "ProjectNumber" in xml
    assert "P-99812" not in xml


def test_app_properties_lose_the_outline_of_the_document():
    pkg = with_extra_parts(build.make())
    R.redact(pkg)
    xml = pkg.blob("docProps/app.xml").decode()
    assert "Thermal Margin Analysis of the X-7" not in xml
    assert "TitlesOfParts" not in xml


# -- fields ---------------------------------------------------------------


FIELD = (
    '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
    '<w:r><w:instrText xml:space="preserve"> DOCPROPERTY "ProjectNumber" '
    '</w:instrText></w:r>'
    '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
    '<w:r><w:t>P-99812</w:t></w:r>'
    '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
)


def test_a_cached_field_result_is_cleared_and_the_field_marked_dirty():
    """The result looks like ordinary text, so nothing else would catch it."""
    pkg = build.make(build.para("Intro", style="Heading1") + FIELD)
    R.redact(pkg)
    xml = pkg.blob(pkg.main_document).decode()
    assert "P-99812" not in xml
    assert 'DOCPROPERTY "ProjectNumber"' in xml, "the field itself was removed"
    assert 'w:dirty="true"' in xml, "Word would redisplay the stale result"


def test_a_nested_fields_separator_does_not_end_the_outer_field():
    """Depth matters: read at depth one and the outer result survives."""
    nested = (
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText> IF </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t>7</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:t>OUTERSECRET</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
    )
    pkg = build.make(nested)
    R.redact(pkg)
    xml = pkg.blob(pkg.main_document).decode()
    assert "OUTERSECRET" not in xml


# -- footnotes ------------------------------------------------------------


def add_footnotes(pkg: OpcPackage, text: str) -> None:
    xml = (
        build.DECL + f'\n<w:footnotes {build.W}>'
        f'<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/>'
        f'</w:r></w:p></w:footnote>'
        f'<w:footnote w:id="2"><w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
        f'</w:footnote></w:footnotes>'
    )
    pkg.add_part("word/footnotes.xml", xml.encode(), FOOTNOTES_CT)
    pkg.relate(RT["footnotes"], "word/footnotes.xml", pkg.main_document)


def test_a_footnote_whose_anchor_was_cleared_goes_with_it(tmp_path):
    """Otherwise the prose survives in a part most people never open."""
    (tmp_path / "corpus").mkdir(parents=True)
    paths = []
    for i in range(3):
        anchored = (f'<w:p><w:pPr><w:pStyle w:val="BodyText"/></w:pPr>'
                    f'<w:r><w:t>{PROSE[i]}</w:t></w:r>'
                    f'<w:r><w:footnoteReference w:id="2"/></w:r></w:p>')
        pkg = exemplar(i, extra_body=anchored)
        add_footnotes(pkg, f"Measured by L. Craig, document {i}.")
        path = tmp_path / "corpus" / f"r{i}.docx"
        pkg.save(path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    notes = text_of(template, "word/footnotes.xml")
    assert "Measured by L. Craig" not in notes


def test_the_separator_footnote_is_never_removed():
    """It is Word's own furniture; removing it changes how notes are drawn."""
    pkg = build.make()
    add_footnotes(pkg, "an unreferenced note")
    R.redact(pkg)
    xml = pkg.blob("word/footnotes.xml").decode()
    assert 'w:type="separator"' in xml
    assert "an unreferenced note" not in xml


# -- headers and footers --------------------------------------------------


def test_a_header_line_the_corpus_does_not_repeat_is_cleared(tmp_path):
    (tmp_path / "corpus").mkdir(parents=True)
    paths = []
    for i in range(3):
        pkg = exemplar(i, footer=f"Report LR-2024-004{i} of the X-7 programme")
        path = tmp_path / "corpus" / f"r{i}.docx"
        pkg.save(path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "LR-2024-0040" not in text_of(template, "word/footer1.xml")


def test_a_header_line_every_exemplar_repeats_is_kept(tmp_path):
    """It is the house footer, and a template without it is not the format."""
    _, template = learned(tmp_path, footer="Acme Laboratories  |  Confidential")
    assert "Confidential" in text_of(template, "word/footer1.xml")


def test_clearing_a_header_line_keeps_its_fields(tmp_path):
    """A header's PAGE field is format; the report number typed beside it is not."""
    (tmp_path / "corpus").mkdir(parents=True)
    paths = []
    for i in range(3):
        pkg = exemplar(i, footer="x")
        footer = pkg.edit("word/footer1.xml")
        paragraph = footer.find(qn("w:p"))
        for child in list(paragraph):
            paragraph.remove(child)
        fragment = etree.fromstring(
            f'<w:p {build.W}>'
            f'<w:r><w:t>Report LR-2024-004{i}</w:t></w:r>'
            f'<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            f'<w:r><w:instrText> PAGE </w:instrText></w:r>'
            f'<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        )
        for child in fragment:
            paragraph.append(child)
        path = tmp_path / "corpus" / f"r{i}.docx"
        pkg.save(path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    xml = template.blob("word/footer1.xml").decode()
    assert "LR-2024-0040" not in xml
    assert "PAGE" in xml, "the page number field was cleared with the text"


# -- orphans --------------------------------------------------------------


def test_media_the_cleared_body_referenced_is_dropped(tmp_path):
    (tmp_path / "corpus").mkdir(parents=True)
    paths = []
    for i in range(3):
        pkg = exemplar(i)
        pkg.add_part("word/media/image1.png", b"\x89PNG\r\n\x1a\nTESTARTICLE",
                     "image/png")
        rid = pkg.relate(RT["image"], "word/media/image1.png", pkg.main_document)
        body = pkg.edit(pkg.main_document).find(qn("w:body"))
        body.append(etree.fromstring(
            f'<w:p {build.W} {build.R}>'
            f'<w:r><w:drawing><a:blip xmlns:a="http://schemas.openxmlformats.org'
            f'/drawingml/2006/main" r:embed="{rid}"/></w:drawing></w:r></w:p>'))
        path = tmp_path / "corpus" / f"r{i}.docx"
        pkg.save(path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "word/media/image1.png" not in template


def test_a_logo_a_header_still_references_survives():
    """Falls out of the rule rather than needing a special case for logos."""
    pkg = build.make()
    build.add_hdrftr(pkg, "header", "header1.xml", "Acme")
    pkg.add_part("word/media/logo.png", b"\x89PNG\r\n\x1a\nLOGO", "image/png")
    rid = pkg.relate(RT["image"], "word/media/logo.png", "word/header1.xml")
    header = pkg.edit("word/header1.xml")
    header.append(etree.fromstring(
        f'<w:p {build.W} {build.R}>'
        f'<w:r><w:drawing><a:blip xmlns:a="http://schemas.openxmlformats.org'
        f'/drawingml/2006/main" r:embed="{rid}"/></w:drawing></w:r></w:p>'))
    R.redact(pkg)
    assert "word/media/logo.png" in pkg


def test_an_external_link_nothing_points_at_any_more_is_dropped():
    """SharePoint targets carry a tenant, a site and often a person."""
    pkg = build.make()
    pkg.touch_rels(pkg.main_document).add(
        RT["hyperlink"],
        "https://contoso.sharepoint.com/sites/x7/lcraig/report.docx",
        external=True)
    pkg.touch_rels(pkg.main_document)
    report = R.redact(pkg)
    assert "contoso.sharepoint.com" not in \
        pkg.blob("word/_rels/document.xml.rels").decode()
    assert report.dropped_links == 1


# -- the case agreement cannot decide ------------------------------------


def test_a_name_in_every_exemplars_footer_is_kept_but_reported(tmp_path):
    """One author's corpus agrees on their own name, and consensus cannot tell.

    Deleting it would be wrong on the many formats where the footer really is
    the house footer, so it is kept and said out loud instead.
    """
    result, template = learned(
        tmp_path, creator="L. Craig",
        footer="Acme Laboratories  |  Prepared by L. Craig")
    assert "L. Craig" in text_of(template, "word/footer1.xml")
    assert any("L. Craig" in note and "delete it in template.docx" in note
               for note in result.notes)


def test_no_note_when_the_scrubbed_name_appears_nowhere_else(tmp_path):
    result, _ = learned(tmp_path, creator="L. Craig",
                        footer="Acme Laboratories  |  Confidential")
    assert not any("delete it in template.docx" in n for n in result.notes)


# -- the template still has to be a valid document ------------------------


def test_the_redacted_template_is_structurally_intact(tmp_path):
    _, template = learned(tmp_path, footer="Acme  |  Confidential")
    assert check_integrity(template) == []


def test_redaction_is_deterministic(tmp_path):
    """Two learns over the same corpus must produce the same template bytes."""
    paths = corpus(tmp_path / "corpus", footer="Acme  |  Confidential")
    learn(paths, tmp_path / "a")
    learn(paths, tmp_path / "b")
    assert (tmp_path / "a" / pio.TEMPLATE).read_bytes() == \
        (tmp_path / "b" / pio.TEMPLATE).read_bytes()


def test_learn_reports_what_it_redacted(tmp_path):
    from click.testing import CliRunner

    paths = corpus(tmp_path / "corpus")
    result = CliRunner().invoke(
        cli, ["learn", *[str(p) for p in paths], "-o", str(tmp_path / "p")])
    assert "redact:" in result.output


# -- the regions the aligner never reaches -------------------------------

V_NS = 'xmlns:v="urn:schemas-microsoft-com:vml"'
WP_NS = ('xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/'
         'wordprocessingDrawing"')
A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def textbox_in_boilerplate(inner: str) -> str:
    """A shape anchored in the paragraph every exemplar shares.

    The paragraph is boilerplate, so body clearing keeps it -- and the plan
    already notes that a text-box cover page defeats alignment entirely, which
    is what makes this the interesting case rather than a contrived one.
    """
    return (f'<w:p {build.W} {V_NS}><w:pPr><w:pStyle w:val="BodyText"/></w:pPr>'
            f'<w:r><w:t>{BOILERPLATE}</w:t></w:r>'
            f'<w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
            f'<w:p><w:r><w:t>{inner}</w:t></w:r></w:p>'
            f'</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>')


def corpus_of(tmp_path, bodies):
    (tmp_path / "corpus").mkdir(parents=True, exist_ok=True)
    paths = []
    for i, extra in enumerate(bodies):
        path = tmp_path / "corpus" / f"r{i}.docx"
        exemplar(i, extra_body=extra).save(path, deterministic=True)
        paths.append(path)
    return paths


def test_text_box_content_the_corpus_does_not_share_is_cleared(tmp_path):
    paths = corpus_of(tmp_path, [
        textbox_in_boilerplate(f"Prepared for Acme under contract {i}")
        for i in range(3)])
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "under contract 0" not in text_of(template)


def test_text_box_content_every_exemplar_shares_is_kept(tmp_path):
    """A text-box cover block IS the format when every report has it."""
    paths = corpus_of(tmp_path, [
        textbox_in_boilerplate("UNCLASSIFIED // FOR OFFICIAL USE ONLY")
        for _ in range(3)])
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "FOR OFFICIAL USE ONLY" in text_of(template)


def picture(rid: str, descr: str, name: str = "Picture 1") -> str:
    return (f'<w:p {build.W} {build.R}><w:pPr><w:pStyle w:val="BodyText"/>'
            f'</w:pPr><w:r><w:t>{BOILERPLATE}</w:t></w:r>'
            f'<w:r><w:drawing {WP_NS}><wp:inline>'
            f'<wp:docPr id="1" name="{name}" descr="{descr}"/>'
            f'<a:graphic {A_NS}><a:graphicData><a:blip r:embed="{rid}"/>'
            f'</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>')


def with_picture(i: int, descr: str) -> OpcPackage:
    pkg = exemplar(i)
    pkg.add_part("word/media/image1.png", b"\x89PNG\r\n\x1a\nX", "image/png")
    rid = pkg.relate(RT["image"], "word/media/image1.png", pkg.main_document)
    body = pkg.edit(pkg.main_document).find(qn("w:body"))
    body.insert(len(body) - 1, etree.fromstring(picture(rid, descr)))
    return pkg


def test_alt_text_only_one_document_has_is_cleared(tmp_path):
    """Alt text is where descriptive prose about a photo actually lives."""
    (tmp_path / "corpus").mkdir(parents=True)
    paths = []
    for i in range(3):
        path = tmp_path / "corpus" / f"r{i}.docx"
        with_picture(i, f"Test article on the bench at Acme, visit {i}").save(
            path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "visit 0" not in template.blob(template.main_document).decode()


def test_alt_text_every_document_shares_is_kept(tmp_path):
    """Removing a logo's alt text would break the template's accessibility."""
    (tmp_path / "corpus").mkdir(parents=True)
    paths = []
    for i in range(3):
        path = tmp_path / "corpus" / f"r{i}.docx"
        with_picture(i, "Acme Laboratories letterhead").save(
            path, deterministic=True)
        paths.append(path)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "Acme Laboratories letterhead" in \
        template.blob(template.main_document).decode()


def test_a_table_description_only_one_document_has_is_cleared(tmp_path):
    def described(i: int) -> str:
        return (f'<w:tbl {build.W}><w:tblPr>'
                f'<w:tblDescription w:val="Margins measured in run {i}"/>'
                f'</w:tblPr><w:tr><w:tc><w:tcPr/><w:p><w:r><w:t>Case</w:t>'
                f'</w:r></w:p></w:tc></w:tr></w:tbl><w:p/>')
    paths = corpus_of(tmp_path, [described(i) for i in range(3)])
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "run 0" not in template.blob(template.main_document).decode()


def test_smart_tags_are_unwrapped_but_their_runs_survive():
    """The wrapper carries what Word decided; the runs carry the words."""
    tagged = (f'<w:p {build.W}><w:pPr><w:pStyle w:val="BodyText"/></w:pPr>'
              f'<w:smartTag w:uri="urn:x" w:element="PersonName">'
              f'<w:smartTagPr><w:attr w:name="who" w:val="Craig, L"/>'
              f'</w:smartTagPr><w:r><w:t>{BOILERPLATE}</w:t></w:r>'
              f'</w:smartTag></w:p>')
    pkg = build.make(tagged)
    R.redact(pkg)
    xml = pkg.blob(pkg.main_document).decode()
    assert "Craig, L" not in xml
    assert "smartTag" not in xml
    assert BOILERPLATE in xml


# -- whole trees a template has no business carrying ---------------------


def test_the_donors_title_does_not_become_the_templates_title(tmp_path):
    """dc:title is what Word offers as the name and what a PDF export writes."""
    paths = corpus(tmp_path / "corpus")
    for path in paths:
        pkg = OpcPackage.open(path)
        core = pkg.edit("docProps/core.xml")
        core.find(qn("dc:title")).text = "Thermal Margin Analysis of the X-7"
        pkg.save(path, deterministic=True)
    learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "X-7" not in template.blob("docProps/core.xml").decode()


def test_the_mail_merge_setup_and_its_data_source_are_removed():
    """w:odso is a connection string: a server, or a path under a profile."""
    pkg = build.make(settings_extra=(
        '<w:mailMerge><w:mainDocumentType w:val="formLetters"/>'
        '<w:odso><w:udl w:val="Data Source=C:\\Users\\lcraig\\people.xlsx"/>'
        '</w:odso></w:mailMerge>'))
    pkg.touch_rels(pkg.main_document).add(
        RT["mailMergeSource"], "file:///C:/Users/lcraig/people.xlsx",
        external=True)
    R.redact(pkg)
    assert "lcraig" not in pkg.blob("word/settings.xml").decode()
    assert "lcraig" not in \
        pkg.blob("word/_rels/document.xml.rels").decode()


def test_macros_activex_and_add_ins_do_not_travel_with_the_format():
    """None of them is format, and all of them run on the next person's box."""
    pkg = build.make()
    pkg.add_part("word/vbaProject.bin", b"MZmacro",
                 "application/vnd.ms-office.vbaProject")
    pkg.relate("http://schemas.microsoft.com/office/2006/relationships/"
               "vbaProject", "word/vbaProject.bin", pkg.main_document)
    pkg.add_part("word/activeX/activeX1.xml", b"<ocx/>", "application/xml")
    pkg.add_part("word/webextensions/webextension1.xml", b"<we/>",
                 "application/xml")
    R.redact(pkg)
    for part in ("word/vbaProject.bin", "word/activeX/activeX1.xml",
                 "word/webextensions/webextension1.xml"):
        assert part not in pkg


def test_quick_parts_do_not_travel_with_the_format():
    """A building block is a whole authored passage, stored out of sight."""
    pkg = build.make()
    pkg.add_part("word/glossary/document.xml", (
        build.DECL + f'\n<w:glossaryDocument {build.W}><w:docParts><w:docPart>'
        '<w:docPartBody><w:p><w:r><w:t>Prepared for Acme</w:t></w:r></w:p>'
        '</w:docPartBody></w:docPart></w:docParts></w:glossaryDocument>'
    ).encode(), "application/xml")
    pkg.relate(RT["glossaryDocument"], "word/glossary/document.xml",
               pkg.main_document)
    R.redact(pkg)
    assert "word/glossary/document.xml" not in pkg


def test_the_printer_and_the_signature_go():
    """printerSettings names a printer and often a UNC path; a signature over
    a document we have just rewritten is invalid anyway, and carries the
    signer's certificate."""
    pkg = build.make()
    pkg.add_part("word/printerSettings/printerSettings1.bin", b"\\\\srv\\prn",
                 "application/octet-stream")
    pkg.relate(RT["printerSettings"],
               "word/printerSettings/printerSettings1.bin", pkg.main_document)
    pkg.add_part("_xmlsignatures/sig1.xml", b"<Signature>L Craig</Signature>",
                 "application/xml")
    pkg.relate(RT["signature"], "_xmlsignatures/sig1.xml", "")
    R.redact(pkg)
    assert "word/printerSettings/printerSettings1.bin" not in pkg
    assert "_xmlsignatures/sig1.xml" not in pkg


# -- kept on purpose, and said out loud ----------------------------------


def test_a_sensitivity_label_is_kept_and_reported(tmp_path):
    """Removing an organisation's protection label is not our decision."""
    paths = corpus(tmp_path / "corpus")
    for path in paths:
        pkg = OpcPackage.open(path)
        pkg.add_part("docMetadata/LabelInfo.xml",
                     b'<labelList><label siteId="acme-guid"/></labelList>',
                     "application/xml")
        pkg.save(path, deterministic=True)
    result = learn(paths, tmp_path / "profile")
    template = OpcPackage.open(tmp_path / "profile" / pio.TEMPLATE)
    assert "docMetadata/LabelInfo.xml" in template
    assert any("sensitivity label" in note for note in result.notes)


def test_image_metadata_on_a_surviving_picture_is_reported_not_stripped():
    """Stripping it means re-encoding somebody's letterhead."""
    pkg = build.make()
    build.add_hdrftr(pkg, "header", "header1.xml", "Acme")
    pkg.add_part("word/media/logo.jpeg", b"\xff\xd8Exif\x00\x00II*GPS\xff\xd9",
                 "image/jpeg")
    rid = pkg.relate(RT["image"], "word/media/logo.jpeg", "word/header1.xml")
    header = pkg.edit("word/header1.xml")
    header.append(etree.fromstring(
        f'<w:p {build.W} {build.R}><w:r><w:drawing {WP_NS}><wp:inline>'
        f'<wp:docPr id="1" name="logo"/><a:graphic {A_NS}><a:graphicData>'
        f'<a:blip r:embed="{rid}"/></a:graphicData></a:graphic>'
        f'</wp:inline></w:drawing></w:r></w:p>'))
    report = R.redact(pkg)
    assert "word/media/logo.jpeg" in pkg
    assert any("EXIF" in note and "GPS" in note for note in report.notes)


# -- the rest of the profile folder --------------------------------------
#
# template.docx is one file in a directory that gets shared whole. The
# sidecars are generated from the same corpus and had the same problem.


def learned_with_values(tmp_path, value="LR-2024-0041"):
    (tmp_path / "corpus").mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(3):
        body = (
            build.para("Thermal Margin Analysis", style="Title")
            + build.para(f"Report No. {value[:-1]}{i}", style="BodyText")
            + build.para(BOILERPLATE, style="BodyText")
            + build.para("Introduction", style="Heading1")
            + build.para(PROSE[i], style="BodyText")
        )
        path = tmp_path / "corpus" / f"Acme quarterly review {i}.docx"
        build.make(body, creator="L. Craig",
                   title="Thermal Margin Analysis of the X-7").save(
            path, deterministic=True)
        paths.append(path)
    return learn(paths, tmp_path / "profile"), tmp_path / "profile"


def read_all(directory):
    return {f.name: f.read_text(errors="replace")
            for f in sorted(directory.rglob("*"))
            if f.is_file() and f.suffix != ".docx"}


def test_the_exemplars_real_values_are_not_written_to_overrides(tmp_path):
    """overrides.yaml is the one file learn never regenerates.

    A real value written here outlives the corpus it came from, in a file
    somebody hand-edits for as long as the format exists.
    """
    _, profile = learned_with_values(tmp_path)
    text = (profile / "overrides.yaml").read_text()
    assert "LR-2024-004" not in text
    assert "looks_like" in text


def test_the_shape_is_kept_because_it_is_what_the_message_needed(tmp_path):
    _, profile = learned_with_values(tmp_path)
    text = (profile / "overrides.yaml").read_text()
    assert "AA-0000-000" in text


def test_masking_keeps_the_shape_and_loses_the_content():
    from formgen.learn.placeholders import mask

    assert mask("LR-2024-0041") == "AA-0000-0000"
    assert mask("L. Craig") == "A. Aaaaa"
    assert mask("12 March 2024") == "00 Aaaaa 0000"


def test_no_exemplar_value_reaches_any_generated_file(tmp_path):
    _, profile = learned_with_values(tmp_path)
    for name, text in read_all(profile).items():
        assert "LR-2024-004" not in text, f"{name} carries an exemplar's value"


def test_the_donors_title_does_not_reach_any_generated_file(tmp_path):
    _, profile = learned_with_values(tmp_path)
    for name, text in read_all(profile).items():
        assert "X-7" not in text, f"{name} carries the donor's title"


def test_exemplar_names_are_confined_to_the_two_audit_files(tmp_path):
    """So that "delete these two before sharing" is a complete instruction.

    Smeared across four files it would not have been -- which is what made
    the advice worth giving only once the names were concentrated.
    """
    _, profile = learned_with_values(tmp_path)
    carrying = {name for name, text in read_all(profile).items()
                if "Acme quarterly review" in text}
    assert carrying <= {"corpus.json", "evidence.json"}


def test_and_the_readme_says_which_two(tmp_path):
    _, profile = learned_with_values(tmp_path)
    readme = (profile / "README.md").read_text()
    assert "corpus.json" in readme and "evidence.json" in readme
    assert "delete both before sharing" in readme
