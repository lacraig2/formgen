"""Rules about which file we are allowed to write, and when.

This tool rewrites other people's documents, so the defaults are conservative
and the dangerous options are explicit:

* **Never write the input path by default.** Output goes to
  `<stem>.formatted.docx` or `--out-dir`.
* `--in-place` implies `--backup`, refuses while Word has the file open, and
  refuses without a TTY unless `--force` -- so a misconfigured CI job cannot
  silently rewrite a repository full of documents.
* Word's lock file is a `~$` sibling. Checking for it turns "PermissionError"
  into "close the document in Word and retry", which is the difference
  between a bug report and a shrug.

The Windows-specific parts are real rather than speculative: `os.replace` onto
a path Word has open **fails on Windows and succeeds on Linux**, deep
SharePoint trees exceed MAX_PATH, and a OneDrive file that has not been
downloaded is a reparse point that reads as empty.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ..errors import RefusalError, UsageError

DEFAULT_SUFFIX = ".formatted"
MAX_PATH = 260


def lock_file(path: Path) -> Path:
    """Word's owner file: `~$` plus the name, truncated as Word truncates it."""
    name = path.name
    return path.with_name("~$" + (name[2:] if len(name) > 8 else name))


def is_open_in_word(path: Path) -> bool:
    """Word drops a ~$ sibling while a document is open.

    Both spellings are checked because Word truncates the stem for longer
    names, and which one it writes depends on the name's length.
    """
    return lock_file(path).exists() or path.with_name("~$" + path.name).exists()


def output_path(
    source: Path,
    out_dir: Path | None = None,
    in_place: bool = False,
    suffix: str = DEFAULT_SUFFIX,
) -> Path:
    source = Path(source)
    if in_place:
        return source
    if out_dir is not None:
        return Path(out_dir) / source.name
    return source.with_name(f"{source.stem}{suffix}{source.suffix}")


def long_path(path: Path) -> str:
    r"""Prefix \\?\ on Windows so deep SharePoint trees do not hit MAX_PATH."""
    text = str(Path(path).absolute())
    if sys.platform != "win32" or len(text) < MAX_PATH or text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def is_cloud_placeholder(path: Path) -> bool:
    """A OneDrive/SharePoint file that has not actually been downloaded.

    It reads as a sparse reparse point, so opening it either stalls on a
    network fetch or yields nothing at all.
    """
    if sys.platform != "win32":
        return False
    try:
        attributes = os.stat(path).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
    FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
    return bool(attributes & (FILE_ATTRIBUTE_RECALL_ON_OPEN
                              | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def check_writable(
    destination: Path, in_place: bool = False, force: bool = False,
    interactive: bool | None = None,
) -> list[str]:
    """Raise if we must not write here; return advisory notes if we may."""
    destination = Path(destination)
    notes: list[str] = []

    if in_place and not force:
        tty = sys.stdin.isatty() if interactive is None else interactive
        if not tty:
            raise UsageError(
                "--in-place refuses to run without a terminal",
                "a misconfigured job could rewrite every document it can "
                "reach. Pass --force if that is genuinely what you want.",
            )
    if destination.exists() and is_open_in_word(destination):
        raise RefusalError(
            f"{destination.name} is open in Word",
            "close the document and run again. Writing underneath Word loses "
            "whatever is unsaved and can corrupt the file.",
        )
    if destination.exists() and not os.access(destination, os.W_OK):
        raise RefusalError(
            f"{destination} is not writable",
            "check the file's permissions, or use --out-dir to write elsewhere.",
        )
    parent = destination.parent
    if not parent.exists():
        raise UsageError(
            f"{parent} does not exist",
            "create the directory, or point --out-dir somewhere that does.",
        )
    if is_cloud_placeholder(destination):
        notes.append(
            f"{destination.name} is a cloud placeholder that has not been "
            "downloaded; use --out-dir to write to a local path instead."
        )
    if len(str(destination.absolute())) >= MAX_PATH and sys.platform == "win32":
        notes.append(
            "the output path is longer than 260 characters; some Windows "
            "tools will not be able to open it."
        )
    return notes


def check_source(path: Path) -> None:
    path = Path(path)
    if not path.exists():
        raise UsageError(f"{path} does not exist")
    if is_open_in_word(path):
        raise RefusalError(
            f"{path.name} is open in Word",
            "close it first: what is on disk is not what you are looking at.",
        )
    if is_cloud_placeholder(path):
        raise RefusalError(
            f"{path.name} has not been downloaded from the cloud",
            "open it once in Explorer to hydrate it, or use 'Always keep on "
            "this device'.",
        )
