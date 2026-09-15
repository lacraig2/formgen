"""The CLI surface: exit codes, ASCII safety, and the messages people read."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from fixtures import build
from formgen.cli import cli
from formgen.profile import io as pio
from formgen.report.console import Console, asciify


@pytest.fixture
def runner():
    return CliRunner()


def corpus(tmp_path, n=6, **kwargs):
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        path = tmp_path / f"report{i}.docx"
        build.make(**kwargs).save(path, deterministic=True)
        out.append(str(path))
    return out


def run(runner, *args):
    return runner.invoke(cli, list(args), obj={})


# -- learn ----------------------------------------------------------------


def test_learn_writes_a_profile_and_says_what_to_do_next(runner, tmp_path):
    result = run(runner, "learn", *corpus(tmp_path), "-o", str(tmp_path / "p"))
    assert result.exit_code == 0, result.output
    assert "formgen profile sync" in result.output
    assert (tmp_path / "p" / pio.PROFILE).exists()


def test_learn_json_output_is_machine_readable(runner, tmp_path):
    result = run(runner, "learn", *corpus(tmp_path), "-o", str(tmp_path / "p"),
                 "--json")
    payload = json.loads(result.output)
    assert payload["donor"] == "report0"
    assert payload["properties"] > 0


def test_learn_exits_1_when_something_needs_review(runner, tmp_path):
    """A profile with contested properties is written, and the exit code says
    a human still has to look at it."""
    paths = corpus(tmp_path / "a", 5) + corpus(tmp_path / "b", 1, default_sz=24)
    result = run(runner, "learn", *paths, "-o", str(tmp_path / "p"))
    assert result.exit_code == 1
    assert "NEEDS REVIEW" in result.output
    assert (tmp_path / "p" / pio.PROFILE).exists()   # still written


def test_learn_refuses_a_corpus_it_cannot_donate_from(runner, tmp_path):
    paths = corpus(tmp_path, 3, settings_extra="<w:trackChanges/>")
    result = run(runner, "learn", *paths, "-o", str(tmp_path / "p"))
    assert result.exit_code == 3
    assert "error:" in result.output
    assert "Track Changes" in result.output


def test_learn_with_no_exemplars_is_a_usage_error(runner, tmp_path):
    result = run(runner, "learn", "-o", str(tmp_path / "p"))
    assert result.exit_code == 2
    assert "no exemplars" in result.output


# -- inspect --------------------------------------------------------------


def test_inspect_reports_what_we_can_see(runner, tmp_path):
    path = tmp_path / "one.docx"
    build.make().save(path, deterministic=True)
    result = run(runner, "inspect", str(path))
    assert result.exit_code == 0
    assert "3 paragraphs" in result.output
    assert "direct formatting: 0%" in result.output


def test_inspect_exits_3_on_a_document_we_would_refuse(runner, tmp_path):
    path = tmp_path / "locked.docx"
    build.make(
        settings_extra='<w:documentProtection w:edit="readOnly" w:enforcement="1"/>'
    ).save(path, deterministic=True)
    result = run(runner, "inspect", str(path))
    assert result.exit_code == 3
    assert "REFUSALS" in result.output
    assert "Stop Protection" in result.output


def test_inspect_on_a_non_docx_exits_4_with_a_diagnosis(runner, tmp_path):
    path = tmp_path / "not.docx"
    path.write_bytes(b"%PDF-1.7\nnot a zip at all")
    result = run(runner, "inspect", str(path))
    assert result.exit_code == 4
    assert "PDF" in result.output


# -- profile --------------------------------------------------------------


def test_profile_show_lists_the_enforced_rules(runner, tmp_path):
    run(runner, "learn", *corpus(tmp_path), "-o", str(tmp_path / "p"))
    result = run(runner, "profile", "show", str(tmp_path / "p"), "--all")
    assert result.exit_code == 0
    assert "/styles/paragraph/body text/run/size" in result.output
    assert "11pt" in result.output


def test_profile_sync_on_an_untouched_template_says_so(runner, tmp_path):
    run(runner, "learn", *corpus(tmp_path), "-o", str(tmp_path / "p"))
    result = run(runner, "profile", "sync", str(tmp_path / "p"))
    assert result.exit_code == 0
    assert "unchanged" in result.output


def test_profile_sync_without_a_template_is_a_usage_error(runner, tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / pio.PROFILE).write_text("{}")
    result = run(runner, "profile", "sync", str(tmp_path / "p"))
    assert result.exit_code == 2


# -- console safety -------------------------------------------------------


def test_report_text_survives_a_cp1252_console():
    """The #1 crash risk on the target machine, and it comes from user text."""
    text = "“DISTRIBUTION” — see § 3–4 • café"
    folded = asciify(text)
    assert folded.encode("cp1252")
    assert folded == '"DISTRIBUTION" -- see ? 3-4 * caf?'


def test_unicode_flag_opts_back_in(runner, tmp_path):
    path = tmp_path / "one.docx"
    build.make(title="x").save(path, deterministic=True)
    result = runner.invoke(cli, ["--unicode", "inspect", str(path)], obj={})
    assert result.exit_code == 0


def test_console_never_dies_on_an_encoding_error():
    class Hostile:
        seen = ""

        def write(self, text):
            if any(ord(c) > 127 for c in text):
                raise UnicodeEncodeError("ascii", text, 0, 1, "nope")
            self.seen += text

        def flush(self):
            pass

    stream = Hostile()
    Console(stream, unicode=True).write("café")
    assert stream.seen.strip() == "caf?"


def test_table_columns_line_up():
    lines: list[str] = []

    class Sink:
        def write(self, text):
            if text != "\n":
                lines.append(text)

        def flush(self):
            pass

    Console(Sink()).table([("a", "1"), ("longer", "22")], ("name", "n"))
    assert lines == ["name    n", "------  --", "a       1", "longer  22"]


# -- explain --------------------------------------------------------------


def test_explain_shows_the_signals_and_the_winner(runner, tmp_path):
    path = tmp_path / "doc.docx"
    build.make().save(path)
    result = run(runner, "explain", str(path), "--at", "Introduction")
    assert result.exit_code == 0, result.output
    assert "SIGNALS" in result.output
    assert "ROLE: heading1" in result.output
    assert "outline level" in result.output


def test_explain_matches_the_way_words_find_box_does(runner, tmp_path):
    """Smart quotes and doubled spaces must not stop a paste from matching."""
    path = tmp_path / "doc.docx"
    build.make(body=build.para("The panel’s  margin was measured.")).save(path)
    result = run(runner, "explain", str(path), "--at", "The panel's margin")
    assert result.exit_code == 0, result.output


def test_explain_says_so_when_the_text_matches_nothing(runner, tmp_path):
    path = tmp_path / "doc.docx"
    build.make().save(path)
    result = run(runner, "explain", str(path), "--at", "not in this document")
    assert result.exit_code == 2
    assert "Find box" in result.output


def test_explain_compares_against_a_profile_when_given_one(runner, tmp_path):
    run(runner, "learn", *corpus(tmp_path / "c"), "-o", str(tmp_path / "p"))
    path = tmp_path / "doc.docx"
    build.make(body=build.para("Some prose that runs on for a few words.")).save(path)
    result = run(runner, "explain", str(path), "--at", "Some prose",
                 "-p", str(tmp_path / "p"))
    assert result.exit_code == 0, result.output
    assert "AGAINST THE PROFILE" in result.output
    assert "restyle" in result.output


# -- learn: structure -----------------------------------------------------


def varied_corpus(tmp_path, n=4):
    """Exemplars that share a format but not their field values."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        body = (
            build.para("Thermal Margin Analysis", style="Title")
            + build.para("Distribution Statement A: approved for public "
                         "release.", style="BodyText")
            + build.para(f"Report No. LR-202{i}-0041", style="BodyText")
            + build.para("Introduction", style="Heading1")
            + build.para("The X-7 radiator exceeds its design margin.",
                         style="BodyText")
        )
        path = tmp_path / f"report{i}.docx"
        build.make(body).save(path, deterministic=True)
        out.append(str(path))
    return out


def test_learn_reports_the_structure_it_found(runner, tmp_path):
    result = run(runner, "learn", *varied_corpus(tmp_path / "c"),
                 "-o", str(tmp_path / "p"))
    assert "structure:" in result.output
    assert "PLACEHOLDERS" in result.output
    assert "content controls" in result.output


def test_a_guessed_placeholder_name_fails_the_build_but_writes_it(runner, tmp_path):
    """A human is forced through this review exactly once, rather than never."""
    result = run(runner, "learn", *varied_corpus(tmp_path / "c"),
                 "-o", str(tmp_path / "p"))
    assert result.exit_code == 1, result.output
    assert "CONFIRM THESE NAMES" in result.output
    assert (tmp_path / "p" / pio.PROFILE).exists()
    assert (tmp_path / "p" / pio.TEMPLATE).exists()


def test_learn_json_carries_the_skeleton(runner, tmp_path):
    result = run(runner, "learn", "--json", *varied_corpus(tmp_path / "c"),
                 "-o", str(tmp_path / "p"))
    payload = json.loads(result.output)
    assert payload["skeleton"]["slots"]
    assert payload["unconfident"]


# -- fill -----------------------------------------------------------------


def form_doc(tmp_path, passport=""):
    body = (
        build.para("VISA APPLICATION FORM")
        + build.table(
            build.cell(build.para("07 -   Passport #")),
            build.cell(build.para("", runs=build.form_text("Text42", passport))),
        )
        + build.para("", runs=(
            build.form_checkbox("Check1") + '<w:r><w:t>male</w:t></w:r>'))
    )
    path = tmp_path / "form.docx"
    build.make(body).save(path, deterministic=True)
    return path


def test_fill_lists_the_fields_without_changing_anything(runner, tmp_path):
    path = form_doc(tmp_path)
    result = run(runner, "fill", str(path), "--list")
    assert result.exit_code == 0, result.output
    assert "passport" in result.output and "male" in result.output
    assert not (tmp_path / "form.filled.docx").exists()


def test_fill_writes_the_values_and_keeps_the_form(runner, tmp_path):
    path = form_doc(tmp_path)
    result = run(runner, "fill", str(path), "--set", "passport=PT-4471902",
                 "--set", "male=yes")
    assert result.exit_code == 0, result.output
    assert "2 field(s) filled" in result.output
    out = tmp_path / "form.filled.docx"
    assert out.exists()
    from formgen.oox.walk import Walker
    from formgen.opc.package import OpcPackage
    text = "\n".join(b.text for b in Walker(OpcPackage.open(out)).blocks())
    assert "VISA APPLICATION FORM" in text and "PT-4471902" in text


def test_fill_says_which_values_matched_nothing(runner, tmp_path):
    path = form_doc(tmp_path)
    result = run(runner, "fill", str(path), "--set", "passport=x",
                 "--set", "nosuchfield=y")
    assert result.exit_code == 1
    assert "nosuchfield" in result.output
    assert "--list" in result.output


def test_fill_needs_something_to_write(runner, tmp_path):
    result = run(runner, "fill", str(form_doc(tmp_path)))
    assert result.exit_code == 2
    assert "--set" in result.output


def test_fill_reads_values_from_a_file(runner, tmp_path):
    path = form_doc(tmp_path)
    values = tmp_path / "v.yaml"
    values.write_text("passport: PT-4471902\nmale: true\n", encoding="utf-8")
    result = run(runner, "fill", str(path), "--values", str(values))
    assert result.exit_code == 0, result.output
    assert "2 field(s) filled" in result.output


def test_fill_refuses_to_keep_a_templates_own_values(runner, tmp_path):
    """template.docx is a real document. --keep-unsupplied there would ship
    the donor's answers under somebody else's name."""
    run(runner, "learn", *varied_corpus(tmp_path / "c"), "-o", str(tmp_path / "p"))
    result = run(runner, "fill", "-p", str(tmp_path / "p"), "--keep-unsupplied",
                 "--set", "report_no=LR-2099-0001")
    assert result.exit_code == 3
    assert "byte-faithful" in result.output


def test_fill_works_on_placeholders_the_comparator_found(runner, tmp_path):
    """A report declares no fields. The comparator finds what varies, learn
    writes those into the template as controls, and they fill like any other."""
    run(runner, "learn", *varied_corpus(tmp_path / "c"), "-o", str(tmp_path / "p"))
    listed = run(runner, "fill", "-p", str(tmp_path / "p"), "--list")
    assert "report_no" in listed.output

    out = tmp_path / "filled.docx"
    result = run(runner, "fill", "-p", str(tmp_path / "p"),
                 "--set", "report_no=LR-2099-0001", "-o", str(out))
    assert result.exit_code == 0, result.output
    from formgen.oox.walk import Walker
    from formgen.opc.package import OpcPackage
    text = "\n".join(b.text for b in Walker(OpcPackage.open(out)).blocks())
    assert "LR-2099-0001" in text
    assert "Thermal Margin Analysis" in text     # the document is still there
