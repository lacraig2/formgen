"""A byte-faithful OPC package.

The preservation guarantee this project rests on: **parts we do not explicitly
edit are written back byte-for-byte**. Only parts handed to :meth:`edit` are
reparsed and reserialized. That is what lets us graft a format onto someone's
document without disturbing comments, footnotes, embedded objects, or any of
the dozens of parts we have never heard of.

A general-purpose docx library cannot offer this, because modelling the package
on load means reserializing content types, relationship ordering and namespace
declarations for parts we never meant to touch.

Relationship parts are a **closed namespace**: they are modelled only as
:class:`Relationships`, never as an element tree. Allowing both would let the
two caches disagree about which is authoritative, and relationship edits would
vanish silently.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import time
import zipfile
from pathlib import Path

from lxml import etree

from .content_types import PART_NAME as CT_PART
from .content_types import ContentTypes, fold
from .errors import PackageError, TargetError
from .ns import RT
from .rels import Relationships, encode_target, rels_name_for, source_of_rels

# A fixed timestamp for reproducible archives. 1980-01-01 is the zip epoch.
FIXED_DATE = (1980, 1, 1, 0, 0, 0)

# Guards against decompression bombs. A large real document with embedded
# video runs to a few hundred MB; beyond that we want a clear refusal, not a
# process that quietly eats all available memory.
MAX_TOTAL_UNCOMPRESSED = 2 * 1024**3
MAX_COMPRESSION_RATIO = 200

_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SAVE_RETRIES = 5


def _normalize_partname(name: str) -> str:
    """Zip entry name -> canonical OPC part name."""
    return name.replace("\\", "/").lstrip("/")


def _diagnose_non_zip(path: Path) -> str:
    """Say what the file actually is, rather than only what it is not."""
    try:
        head = path.open("rb").read(8192)
    except OSError:
        return "could not be read"
    if head[:8] == _CFB_MAGIC:
        # CFB directory entry names are UTF-16LE, so we can tell an encrypted
        # OOXML package from a genuine Word 97-2003 binary without a parser.
        if "EncryptedPackage".encode("utf-16-le") in head:
            return (
                "is an encrypted/password-protected Office document. "
                "Remove the password in Word and save again."
            )
        if "WordDocument".encode("utf-16-le") in head:
            return (
                "is a Word 97-2003 binary .doc file. "
                "Open it in Word and Save As .docx."
            )
        return "is an OLE2 compound file, not a .docx package."
    if head.lstrip()[:5] == b"<?xml":
        if b"pkg:package" in head:
            return (
                "is a Flat OPC (.xml) document. "
                "Open it in Word and Save As .docx."
            )
        return "is a plain XML file, not a .docx package."
    if head[:4] == b"%PDF":
        return "is a PDF, not a .docx package."
    return "is not a zip archive."


class OpcPackage:
    """An OPC (zip) package with lazy, opt-in XML parsing."""

    def __init__(self, blobs: dict[str, bytes], source: Path | None = None):
        normalized: dict[str, bytes] = {}
        folded: dict[str, str] = {}
        for raw_name, data in blobs.items():
            name = _normalize_partname(raw_name)
            key = fold(name)
            if key in folded:
                raise PackageError(
                    f"package contains two parts with equivalent names: "
                    f"{folded[key]!r} and {name!r}. OPC part names compare "
                    f"case-insensitively, so this package is non-conforming."
                )
            folded[key] = name
            normalized[name] = data
        if CT_PART not in normalized:
            raise PackageError(f"package has no {CT_PART}; not a valid OPC file")
        self._blobs = normalized
        self._folded = folded
        self._trees: dict[str, etree._Element] = {}
        self._cache: dict[str, bytes] = {}        # serialized bytes for dirty parts
        self._dirty: set[str] = set()
        self._rels: dict[str, Relationships] = {}
        self.source = source
        self.content_types = ContentTypes.parse(normalized[CT_PART])

    # -- construction ---------------------------------------------------

    @classmethod
    def open(cls, path: str | Path) -> OpcPackage:
        path = Path(path)
        try:
            with zipfile.ZipFile(path) as zf:
                cls._check_size(zf, path)
                blobs: dict[str, bytes] = {}
                seen: dict[str, str] = {}
                for info in zf.infolist():
                    if info.is_dir() or info.filename.endswith("/"):
                        continue
                    name = cls._entry_name(info)
                    key = fold(_normalize_partname(name))
                    if key in seen:
                        raise PackageError(
                            f"{path} contains more than one entry named "
                            f"{seen[key]!r}; a part would be silently discarded."
                        )
                    seen[key] = name
                    # Read by ZipInfo, not by name: it seeks to the header
                    # offset and so is immune to name ambiguity.
                    blobs[name] = zf.read(info)
        except zipfile.BadZipFile as exc:
            raise PackageError(f"{path} {_diagnose_non_zip(path)}") from exc
        except NotImplementedError as exc:
            raise PackageError(
                f"{path} uses an unsupported compression method "
                f"(Deflate64 and similar are not readable by Python's zipfile). "
                f"Re-save the document from Word."
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise PackageError(f"{path} could not be opened: {exc}") from exc
        return cls(blobs, source=path)

    @staticmethod
    def _entry_name(info: zipfile.ZipInfo) -> str:
        """Recover the entry name, undoing zipfile's CP437 fallback decode.

        When general-purpose bit 11 is clear, zipfile decodes names as CP437
        but writes them back as UTF-8, silently changing the bytes. Producers
        that omit the flag include 7-Zip and several Java packagers, and the
        result is an image whose relationship Target matches nothing.
        """
        if info.flag_bits & 0x800:
            return info.filename
        try:
            return info.filename.encode("cp437").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return info.filename

    @staticmethod
    def _check_size(zf: zipfile.ZipFile, path: Path) -> None:
        """Reject decompression bombs using the central directory only."""
        total = 0
        for info in zf.infolist():
            total += info.file_size
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESSION_RATIO and info.file_size > 10 * 1024**2:
                    raise PackageError(
                        f"{path}: entry {info.filename!r} expands {ratio:.0f}x "
                        f"({info.file_size} bytes); refusing to open."
                    )
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise PackageError(
                f"{path}: package expands to {total} bytes, over the "
                f"{MAX_TOTAL_UNCOMPRESSED} byte limit; refusing to open."
            )

    # -- part access ----------------------------------------------------

    @staticmethod
    def is_rels_part(partname: str) -> bool:
        return partname.lower().endswith(".rels")

    def __contains__(self, partname: str) -> bool:
        return fold(_normalize_partname(partname)) in self._folded

    def actual_name(self, partname: str) -> str | None:
        """The package's own spelling of a part name, matched case-insensitively."""
        return self._folded.get(fold(_normalize_partname(partname)))

    def names(self) -> list[str]:
        return list(self._blobs)

    def blob(self, partname: str) -> bytes:
        """Current bytes for a part, reserializing first if it has been edited."""
        name = self.actual_name(partname)
        if name is None:
            raise PackageError(f"no such part: {partname}")
        if name in self._dirty:
            if name not in self._cache:
                self._cache[name] = self._serialize(name)
            return self._cache[name]
        return self._blobs[name]

    def element(self, partname: str) -> etree._Element:
        """Parse a part for **reading**. The part is not marked for rewrite."""
        name = self.actual_name(partname)
        if name is None:
            raise PackageError(f"no such part: {partname}")
        if self.is_rels_part(name):
            raise PackageError(
                f"{name} is a relationship part; use rels()/touch_rels() instead. "
                f"Modelling it as an element tree too would let the two "
                f"representations disagree and silently drop relationship edits."
            )
        if name not in self._trees:
            try:
                self._trees[name] = etree.fromstring(self._blobs[name])
            except etree.XMLSyntaxError as exc:
                raise PackageError(f"{name} is not well-formed XML: {exc}") from exc
        return self._trees[name]

    def edit(self, partname: str) -> etree._Element:
        """Parse a part for **modification**, marking it to be reserialized."""
        el = self.element(partname)
        name = self.actual_name(partname)
        self._dirty.add(name)
        self._cache.pop(name, None)
        return el

    def touch(self, partname: str) -> None:
        """Mark an already-parsed part as modified (invalidates its cache)."""
        name = self.actual_name(partname)
        if name is None:
            raise PackageError(f"no such part: {partname}")
        self._dirty.add(name)
        self._cache.pop(name, None)

    def is_dirty(self, partname: str) -> bool:
        return self.actual_name(partname) in self._dirty

    def _serialize(self, partname: str) -> bytes:
        if partname == CT_PART:
            return self.content_types.serialize()
        if self.is_rels_part(partname):
            rels = self._rels.get(partname)
            if rels is None:
                return self._blobs[partname]
            return rels.serialize()
        return etree.tostring(
            self._trees[partname],
            xml_declaration=True,
            encoding="UTF-8",
            standalone=True,
        )

    # -- part mutation --------------------------------------------------

    def add_part(
        self, partname: str, blob: bytes, content_type: str | None = None
    ) -> None:
        partname = _normalize_partname(partname)
        if partname in self:
            raise PackageError(f"part already exists: {partname}")
        if clash := self._prefix_clash(partname):
            # "The name of a part shall not be derivable from the name of
            # another part by appending segments to it." Such a pair cannot be
            # extracted to a filesystem, which is how Word and every repair
            # tool will choke on it.
            raise PackageError(
                f"cannot add {partname!r}: its name is derivable from "
                f"{clash!r} (or vice versa), which OPC forbids."
            )
        self._blobs[partname] = blob
        self._folded[fold(partname)] = partname
        if content_type:
            self.content_types.set_override(partname, content_type)
        else:
            self.content_types.ensure_media_default(partname)
        self._touch_content_types()

    def replace_part(self, partname: str, blob: bytes) -> None:
        name = self.actual_name(partname) or _normalize_partname(partname)
        self._blobs[name] = blob
        self._folded[fold(name)] = name
        self._trees.pop(name, None)
        self._rels.pop(name, None)
        self._cache.pop(name, None)
        self._dirty.discard(name)
        if name == CT_PART:
            # Otherwise the stale in-memory map would be reserialized over the
            # replacement the moment anything dirties content types.
            self.content_types = ContentTypes.parse(blob)

    def drop_part(self, partname: str, with_rels: bool = True) -> None:
        """Remove a part, its content-type override, its rels, and any
        relationship pointing AT it.

        Dropping only downward leaves an incoming relationship resolving to
        nothing, which is exactly what produces Word's "unreadable content"
        repair prompt.
        """
        name = self.actual_name(partname)
        if name is None:
            return
        self._blobs.pop(name, None)
        self._folded.pop(fold(name), None)
        self._trees.pop(name, None)
        self._rels.pop(name, None)
        self._cache.pop(name, None)
        self._dirty.discard(name)
        if self.content_types.has_override(name):
            self.content_types.drop(name)
            self._touch_content_types()
        self._drop_incoming_rels(name)
        if with_rels:
            rname = rels_name_for(name)
            if rname in self:
                self.drop_part(rname, with_rels=False)

    def _prefix_clash(self, partname: str) -> str | None:
        """An existing part whose name is a segment-prefix of this one, or vice versa."""
        candidate = fold(partname)
        for existing in self._folded:
            if existing == candidate:
                continue
            shorter, longer = sorted((existing, candidate), key=len)
            if longer.startswith(shorter + "/"):
                return self._folded[existing]
        return None

    def _drop_incoming_rels(self, partname: str) -> None:
        for source in self._rels_sources():
            rels = self.rels(source)
            doomed = [r.rid for r in rels if r.resolve(source) == partname]
            if doomed:
                live = self.touch_rels(source)
                for rid in doomed:
                    live.drop(rid)

    def _rels_sources(self) -> list[str]:
        out = []
        for name in list(self._blobs):
            if not self.is_rels_part(name):
                continue
            if posixpath.basename(posixpath.dirname(name)) != "_rels":
                continue
            out.append(source_of_rels(name))
        return out

    def unique_partname(self, template: str) -> str:
        """Mint an unused part name from a '{n}' template, case-insensitively.

        Case matters: minting word/media/Image1.png beside an existing
        image1.png is legal in the zip but confuses Windows tooling, and the
        collision is invisible on a case-sensitive dev filesystem.
        """
        n = 1
        while True:
            candidate = template.replace("{n}", str(n))
            if fold(candidate) not in self._folded:
                return candidate
            n += 1

    def _touch_content_types(self) -> None:
        self._dirty.add(CT_PART)
        self._cache.pop(CT_PART, None)

    # -- relationships --------------------------------------------------

    def rels(self, partname: str = "") -> Relationships:
        """Relationships owned by a part ('' for the package root)."""
        rname = rels_name_for(partname)
        actual = self.actual_name(rname) or rname
        if actual not in self._rels:
            if actual in self._blobs:
                self._rels[actual] = Relationships.parse(partname, self._blobs[actual])
            else:
                self._rels[actual] = Relationships(partname)
        return self._rels[actual]

    def touch_rels(self, partname: str = "", allow_missing: bool = False) -> Relationships:
        """Relationships, marked to be reserialized on save."""
        if partname and partname not in self and not allow_missing:
            raise PackageError(
                f"cannot add relationships for missing part {partname!r}; "
                f"a .rels part with no source part is not valid OPC."
            )
        rels = self.rels(partname)
        rname = self.actual_name(rels_name_for(partname)) or rels_name_for(partname)
        self._dirty.add(rname)
        self._cache.pop(rname, None)
        if rname not in self._blobs:
            self._blobs[rname] = rels.serialize()
            self._folded[fold(rname)] = rname
        self.content_types.ensure_default(
            "rels", "application/vnd.openxmlformats-package.relationships+xml"
        )
        return rels

    def relate(self, reltype: str, target_partname: str, source: str = "") -> str:
        """Relate `source` to a part, encoding the Target correctly."""
        return self.touch_rels(source).add(
            reltype, encode_target(target_partname, source)
        )

    # -- well-known parts, resolved through relationships ---------------

    @property
    def main_document(self) -> str:
        name = self.rels().part_of_type(RT["officeDocument"])
        if not name:
            raise PackageError(
                "package root has no officeDocument relationship; "
                "this is not a WordprocessingML document"
            )
        actual = self.actual_name(name)
        if actual is None:
            raise PackageError(
                f"the officeDocument relationship points at {name!r}, "
                f"which is not in the package."
            )
        return actual

    def related(self, reltype: str, source: str | None = None) -> str | None:
        """Resolve a part reached from `source` (default: the main document)."""
        src = self.main_document if source is None else source
        target = self.rels(src).part_of_type(reltype)
        return self.actual_name(target) if target else None

    def related_all(self, reltype: str, source: str | None = None) -> list[str]:
        src = self.main_document if source is None else source
        rels = self.rels(src)
        out = []
        for rel in rels.of_type(reltype):
            if (target := rel.resolve(src)) and (actual := self.actual_name(target)):
                out.append(actual)
        return out

    # -- integrity ------------------------------------------------------

    def dangling_rels(self) -> list[tuple[str, str, str]]:
        """(source part, rId, target) for every internal rel resolving nowhere.

        Read-only: it does not populate the relationship cache, so a later
        touch_rels() cannot pick up a bogus entry this scan created.
        """
        bad: list[tuple[str, str, str]] = []
        for name in list(self._blobs):
            if not self.is_rels_part(name):
                continue
            if posixpath.basename(posixpath.dirname(name)) != "_rels":
                continue
            src = source_of_rels(name)
            cached = self._rels.get(name)
            try:
                # `cached or parse(...)` reads the stale blob whenever the
                # cached collection is EMPTY, because Relationships defines
                # __len__ and an empty one is falsy. Dropping the last
                # relationship from a part is exactly when this matters: the
                # in-memory rels are right, the check re-reads the original
                # bytes, and save refuses to write a package that is fine.
                rels = (cached if cached is not None
                        else Relationships.parse(src, self._blobs[name]))
            except TargetError as exc:
                bad.append((src, "-", str(exc)))
                continue
            for rel in rels:
                if rel.external:
                    continue
                target = rel.resolve(src)
                if target is None or target not in self:
                    bad.append((src, rel.rid, rel.target))
        return bad

    def missing_content_types(self) -> list[str]:
        return self.content_types.validate(self._blobs)

    def check(self) -> list[str]:
        """Every structural fault that would make Word refuse or repair."""
        faults = [
            f"part has no content type: {name}"
            for name in self.missing_content_types()
        ]
        faults += [
            f"relationship {rid} in {src} points at missing target {target!r}"
            for src, rid, target in self.dangling_rels()
        ]
        return faults

    def sha256(self) -> str:
        h = hashlib.sha256()
        for name in sorted(self._blobs):
            h.update(name.encode("utf-8"))
            h.update(self.blob(name))
        return h.hexdigest()

    # -- output ---------------------------------------------------------

    def save(
        self,
        path: str | Path,
        deterministic: bool = False,
        check: bool = True,
    ) -> None:
        """Write the package. Untouched parts are copied byte-for-byte.

        `deterministic` pins every entry timestamp so two runs over the same
        input produce byte-identical archives -- required for golden tests.
        `check` refuses to write a structurally broken package.
        """
        path = Path(path)
        if check and (faults := self.check()):
            listed = "\n  ".join(faults[:10])
            more = f"\n  ... and {len(faults) - 10} more" if len(faults) > 10 else ""
            raise PackageError(
                f"refusing to write a package Word would reject:\n  {listed}{more}"
            )

        # A short temp name with a .tmp extension: sync clients ignore it, and
        # it adds few characters to a path that may be near MAX_PATH already.
        tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")
        try:
            with open(tmp, "wb") as fh:
                with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as zf:
                    for name in self._ordered_names():
                        data = self.blob(name)
                        if deterministic:
                            info = zipfile.ZipInfo(name, date_time=FIXED_DATE)
                            info.compress_type = zipfile.ZIP_DEFLATED
                            info.external_attr = 0o600 << 16
                            zf.writestr(info, data)
                        else:
                            zf.writestr(name, data)
                # Without this, a power loss can leave a directory entry
                # pointing at unwritten blocks -- the original replaced by
                # garbage. "Atomic" means nothing if the data is not on disk.
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            # A partially written output is never left lying around: it is
            # not a document, and a .tmp beside a report is something a user
            # will eventually double-click.
            tmp.unlink(missing_ok=True)
            raise

        # Deliberately outside the block above. By here the temp file is
        # complete and fsynced, and it holds the only copy of the work -- so
        # if the rename fails because Word has the destination open, the
        # error names the temp file and the temp file is still there. It said
        # "your output is preserved at ..."; it has to be true.
        self._replace_with_retry(tmp, path)

    @staticmethod
    def _replace_with_retry(tmp: Path, path: Path) -> None:
        """os.replace, retried.

        On Windows this fails with a sharing violation when Word has the
        document open, and transiently when antivirus or a sync client is
        holding the file we just wrote.
        """
        last: OSError | None = None
        for attempt in range(_SAVE_RETRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:
                last = exc
                time.sleep(0.2 * (attempt + 1))
            except OSError as exc:
                last = exc
                break
        raise PackageError(
            f"could not write {path}: {last}. "
            f"If the document is open in Word, close it and retry. "
            f"Your output is preserved at {tmp}."
        ) from last

    def _ordered_names(self) -> list[str]:
        """Original zip order, with [Content_Types].xml first and new parts last."""
        names = list(self._blobs)
        names.sort(key=lambda n: (n != CT_PART,))
        return names

    def clone(self) -> OpcPackage:
        """A deep-enough copy to mutate independently of this one."""
        blobs = {name: self.blob(name) for name in self._ordered_names()}
        return OpcPackage(blobs, source=self.source)


def part_dir(partname: str) -> str:
    return posixpath.dirname(partname)
