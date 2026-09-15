"""Backups and undo, with a manifest that makes undo safe to offer.

The manifest records the hash of what we wrote. `undo` restores **only if the
file on disk still matches it** -- if the user has edited since, undoing would
throw their work away, so it refuses and explains instead. An undo that can
destroy work is worse than no undo at all, because people trust it.
"""

from __future__ import annotations

import datetime as _dt
import json
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import RefusalError, UsageError
from ..profile.io import sha256_of

MANIFEST_SUFFIX = ".formgen.json"


@dataclass
class Manifest:
    input: str
    output: str
    backup: str
    input_sha256: str
    output_sha256: str
    profile: str = ""
    profile_sha256: str = ""
    version: str = ""
    run_id: str = ""
    when: str = ""

    def write(self, path: Path) -> Path:
        path.write_text(
            json.dumps(asdict(self), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path) -> Manifest:
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


def timestamp(now: _dt.datetime | None = None) -> str:
    return (now or _dt.datetime.now()).strftime("%Y%m%d-%H%M%S")


def backup_path(source: Path, when: str | None = None) -> Path:
    stamp = when or timestamp()
    return Path(source).with_name(f"{Path(source).stem}.{stamp}.bak{Path(source).suffix}")


def make_backup(source: Path, when: str | None = None) -> Path:
    """Copy the original aside, preserving its timestamps."""
    destination = backup_path(source, when)
    shutil.copy2(source, destination)
    return destination


def manifest_path(output: Path) -> Path:
    return Path(output).with_name(Path(output).name + MANIFEST_SUFFIX)


def write_manifest(
    source: Path, output: Path, backup: Path | None, profile: str = "",
    profile_sha256: str = "", version: str = "", run_id: str = "",
    when: str | None = None,
) -> Path:
    manifest = Manifest(
        input=str(source),
        output=str(output),
        backup=str(backup) if backup else "",
        input_sha256=sha256_of(backup) if backup else "",
        output_sha256=sha256_of(output),
        profile=profile,
        profile_sha256=profile_sha256,
        version=version,
        run_id=run_id or timestamp(),
        when=when or _dt.datetime.now().replace(microsecond=0).isoformat(),
    )
    return manifest.write(manifest_path(output))


def undo(output: Path) -> Path:
    """Put the original back, refusing if the user has edited since."""
    output = Path(output)
    path = manifest_path(output)
    if not path.exists():
        raise UsageError(
            f"no formgen manifest beside {output.name}",
            "undo only works on a file formgen wrote with --backup.",
        )
    manifest = Manifest.read(path)
    if not manifest.backup or not Path(manifest.backup).exists():
        raise UsageError(
            f"the backup named in {path.name} is gone ({manifest.backup!r})",
            "nothing can be restored.",
        )
    if not output.exists():
        shutil.copy2(manifest.backup, output)
        return output
    if sha256_of(output) != manifest.output_sha256:
        raise RefusalError(
            f"{output.name} has changed since formgen wrote it",
            "undoing would discard those edits. Move the file aside first if "
            f"you really want the original back; it is at {manifest.backup}.",
        )
    shutil.copy2(manifest.backup, output)
    return output
