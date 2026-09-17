"""The Windows edges, exercised on Linux.

Every one of these is a failure that cannot happen on the development
machine and happens constantly on the target: Word holding the file open,
`MAX_PATH`, OneDrive placeholders, a cp1252 console. They are tested by
driving the platform checks directly rather than by trusting that the
Windows job will catch them, because the Windows job runs last.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from fixtures import build
from formgen.errors import RefusalError, UsageError
from formgen.opc import package as package_module
from formgen.opc.package import OpcPackage
from formgen.report.console import Console, asciify
from formgen.safety import guards


# -- Word holds the file open --------------------------------------------


def test_the_lock_file_is_recognised_in_both_of_words_spellings(tmp_path):
    """Word truncates the stem for longer names, and which spelling it writes
    depends on the length -- so both are checked."""
    short = tmp_path / "ab.docx"
    long = tmp_path / "quarterly-report.docx"
    (tmp_path / "~$ab.docx").write_bytes(b"")
    (tmp_path / "~$arterly-report.docx").write_bytes(b"")
    assert guards.is_open_in_word(short)
    assert guards.is_open_in_word(long)


def test_writing_under_an_open_document_is_refused_with_a_remedy(tmp_path):
    destination = tmp_path / "report.docx"
    destination.write_bytes(b"x")
    (tmp_path / "~$report.docx").write_bytes(b"")
    with pytest.raises(RefusalError) as caught:
        guards.check_writable(destination)
    assert "open in Word" in str(caught.value)
    assert "close the document" in caught.value.remedy


def test_reading_an_open_document_is_refused_too(tmp_path):
    source = tmp_path / "report.docx"
    source.write_bytes(b"x")
    (tmp_path / "~$report.docx").write_bytes(b"")
    with pytest.raises(RefusalError) as caught:
        guards.check_source(source)
    assert "not what you are looking at" in caught.value.remedy


# -- in place, without a terminal ----------------------------------------


def test_in_place_refuses_without_a_terminal(tmp_path):
    """A misconfigured CI job must not quietly rewrite a whole repository."""
    with pytest.raises(UsageError) as caught:
        guards.check_writable(tmp_path / "x.docx", in_place=True,
                              interactive=False)
    assert "--force" in caught.value.remedy


def test_force_is_how_you_mean_it(tmp_path):
    destination = tmp_path / "x.docx"
    assert guards.check_writable(destination, in_place=True, force=True,
                                 interactive=False) == []


# -- output paths --------------------------------------------------------


def test_output_lands_beside_the_input_by_default(tmp_path):
    assert guards.output_path(tmp_path / "a.docx").name == "a.formatted.docx"


def test_out_dir_keeps_the_original_name(tmp_path):
    out = guards.output_path(tmp_path / "a.docx", out_dir=tmp_path / "out")
    assert out == tmp_path / "out" / "a.docx"


def test_a_missing_output_directory_is_a_usage_error_not_a_traceback(tmp_path):
    with pytest.raises(UsageError) as caught:
        guards.check_writable(tmp_path / "nope" / "x.docx")
    assert "does not exist" in str(caught.value)


# -- MAX_PATH ------------------------------------------------------------


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="asserts the off-Windows path; on Windows the UNC "
                           "prefix is correct and has its own test")
def test_a_long_path_is_left_alone_off_windows(tmp_path):
    deep = tmp_path / ("d" * 120) / ("e" * 200) / "x.docx"
    assert not guards.long_path(deep).startswith("\\\\?\\")


def test_a_long_path_gets_the_unc_prefix_on_windows(monkeypatch):
    monkeypatch.setattr(guards.sys, "platform", "win32")
    monkeypatch.setattr(Path, "absolute",
                        lambda self: Path("C:/" + "a" * 300 + "/x.docx"))
    assert guards.long_path(Path("x.docx")).startswith("\\\\?\\")


def test_a_unc_share_gets_the_unc_form(monkeypatch):
    monkeypatch.setattr(guards.sys, "platform", "win32")
    monkeypatch.setattr(Path, "absolute",
                        lambda self: Path("\\\\server\\share\\" + "a" * 300))
    assert guards.long_path(Path("x")).startswith("\\\\?\\UNC\\")


def test_an_already_prefixed_path_is_not_prefixed_twice(monkeypatch):
    monkeypatch.setattr(guards.sys, "platform", "win32")
    prefixed = "\\\\?\\C:\\" + "a" * 300
    monkeypatch.setattr(Path, "absolute", lambda self: Path(prefixed))
    assert guards.long_path(Path("x")).count("\\\\?\\") == 1


# -- OneDrive placeholders -----------------------------------------------


class _FakeStat:
    def __init__(self, attributes):
        self.st_file_attributes = attributes


class _FakeOs:
    """Only `stat` is faked. Patching the real os.stat breaks pytest itself,
    which reads source files to render a traceback."""

    def __init__(self, attributes):
        self._attributes = attributes

    def stat(self, path):
        return _FakeStat(self._attributes)

    def __getattr__(self, name):
        return getattr(os, name)


def test_an_undownloaded_cloud_file_is_refused_with_a_remedy(tmp_path, monkeypatch):
    source = tmp_path / "x.docx"
    source.write_bytes(b"x")
    monkeypatch.setattr(guards.sys, "platform", "win32")
    monkeypatch.setattr(guards, "os", _FakeOs(0x00400000))
    with pytest.raises(RefusalError) as caught:
        guards.check_source(source)
    assert "Always keep on this device" in caught.value.remedy


def test_a_hydrated_file_is_not_mistaken_for_a_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(guards.sys, "platform", "win32")
    monkeypatch.setattr(guards, "os", _FakeOs(0x00000020))
    assert not guards.is_cloud_placeholder(tmp_path / "x.docx")


def test_a_filesystem_without_the_attribute_does_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(guards.sys, "platform", "win32")
    monkeypatch.setattr(guards, "os", _FakeOs(None))
    monkeypatch.setattr(_FakeOs, "stat", lambda self, path: object())
    assert not guards.is_cloud_placeholder(tmp_path / "x.docx")


# -- console encoding ----------------------------------------------------


def test_every_character_we_emit_survives_cp1252():
    """The #1 crash on the target: a cp1252 console raising on a box-drawing
    character in a report snippet."""
    sample = (
        "  -> \u2014 \u2018quoted\u2019 \u201cdouble\u201d \u2022 bullet "
        "\u2502 \u251c \u2264 \u00a0 \u2026 \u2192 \u00b1 \u00d7"
    )
    asciified = asciify(sample)
    asciified.encode("cp1252")          # would raise if a character survived


def test_the_console_replaces_rather_than_raising_on_a_hostile_stream():
    class Cp1252Stream:
        def __init__(self):
            self.text = ""

        def write(self, chunk):
            chunk.encode("cp1252")      # the real stream would raise here
            self.text += chunk

        def flush(self):
            pass

    stream = Cp1252Stream()
    console = Console(stream=stream)
    console.write("margin \u2014 12.4 K \u2192 ok")
    assert "12.4 K" in stream.text


def test_unicode_output_is_opt_in():
    class Stream:
        def __init__(self):
            self.text = ""

        def write(self, chunk):
            self.text += chunk

        def flush(self):
            pass

    stream = Stream()
    Console(stream=stream, unicode=True).write("\u2192")
    assert "\u2192" in stream.text


# -- atomic write --------------------------------------------------------


def test_a_failed_replace_keeps_the_output_and_says_where(tmp_path, monkeypatch):
    """os.replace onto a path Word has open fails on Windows and succeeds on
    Linux, so the message matters more than the retry."""
    destination = tmp_path / "out.docx"
    destination.write_bytes(b"old")

    def always_busy(src, dst):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(package_module.os, "replace", always_busy)
    monkeypatch.setattr(package_module, "_SAVE_RETRIES", 1)
    with pytest.raises(Exception) as caught:
        build.make().save(destination)
    message = str(caught.value)
    assert "preserved at" in message or "in use" in message
    assert destination.read_bytes() == b"old"
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers, "the written output should not be thrown away"


def test_a_saved_package_reopens_as_the_same_bytes(tmp_path):
    first, second = tmp_path / "a.docx", tmp_path / "b.docx"
    build.make().save(first, deterministic=True)
    OpcPackage.open(first).save(second, deterministic=True)
    assert first.read_bytes() == second.read_bytes()
