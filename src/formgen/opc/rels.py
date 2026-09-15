"""Relationship parts (_rels/*.rels).

Relationship ids are *part-local*: rId3 in word/document.xml.rels and rId3 in
word/header1.xml.rels are unrelated. Targets are resolved relative to the
directory of the source part, not the package root.
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
from dataclasses import dataclass

from lxml import etree

from .errors import TargetError
from .ns import NS

_RID_RE = re.compile(r"^rId(\d+)$")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_REL_TAG = f"{{{NS['pr']}}}Relationship"

def resolve_target(source_partname: str, target: str) -> str:
    """Resolve an internal relationship Target to an absolute part name.

    **Percent-escapes are deliberately NOT decoded.** OPC part-name equivalence
    is computed over the literal percent-encoded ASCII string, so a Target of
    ``media/image%201.png`` names a zip item literally called
    ``word/media/image%201.png`` -- not ``image 1.png``. Decoding here would
    turn every image with a space in its name into a phantom dangling
    relationship. (Both python-docx and POI got this wrong; POI's 2026 fix was
    to stop decoding.)

    What IS normalized: Windows backslashes, which some generators emit, and
    dot segments, which are clamped at the package root per RFC 3986
    remove_dot_segments -- the same resolution Word performs.
    """
    cleaned = target.replace("\\", "/")
    if _SCHEME_RE.match(cleaned):
        raise TargetError(f"internal relationship target has a URI scheme: {target!r}")
    cleaned, _, _fragment = cleaned.partition("#")
    if not cleaned:
        raise TargetError(f"relationship target is a bare fragment: {target!r}")
    base = "" if cleaned.startswith("/") else posixpath.dirname(source_partname)
    # Joining onto "/" first makes normpath clamp ".." at the root rather than
    # preserving it, which is what the spec's worked example requires.
    return posixpath.normpath(posixpath.join("/", base, cleaned)).lstrip("/")


def encode_target(partname: str, source_partname: str) -> str:
    """Build a relationship Target for a part, percent-encoding as required.

    The safe set is narrow but deliberate: "/" must never be encoded (M1.7)
    and unreserved characters must never be encoded (M1.8).
    """
    base = posixpath.dirname(source_partname)
    relative = posixpath.relpath(partname, base) if base else partname
    return urllib.parse.quote(relative.replace("\\", "/"), safe="/-._~")


def rels_name_for(partname: str) -> str:
    """'word/document.xml' -> 'word/_rels/document.xml.rels'; '' -> '_rels/.rels'."""
    if not partname:
        return "_rels/.rels"
    # "There shall be no relationships from or to a Relationships part."
    # Without this guard a mis-derived source name silently mints
    # word/_rels/_rels/document.xml.rels.rels.
    if partname.lower().endswith(".rels"):
        raise TargetError(
            f"{partname} is a relationship part; relationship parts cannot "
            f"themselves have relationships."
        )
    directory, _, base = partname.rpartition("/")
    if directory:
        return f"{directory}/_rels/{base}.rels"
    return f"_rels/{base}.rels"


def source_of_rels(rels_partname: str) -> str:
    """Inverse of :func:`rels_name_for`."""
    directory, _, base = rels_partname.rpartition("/")
    base = base[: -len(".rels")] if base.endswith(".rels") else base
    directory = directory[: -len("/_rels")] if directory.endswith("_rels") else ""
    if directory in ("", "_rels"):
        return base
    return f"{directory}/{base}" if base else directory


@dataclass(frozen=True)
class Relationship:
    rid: str
    reltype: str
    target: str
    external: bool = False

    def resolve(self, source_partname: str) -> str | None:
        """Absolute part name this points at, or None when external.

        Returns None rather than raising for a malformed internal target, so
        that integrity reporting can list it instead of aborting the load.
        """
        if self.external:
            return None
        try:
            return resolve_target(source_partname, self.target)
        except TargetError:
            return None

    def resolve_strict(self, source_partname: str) -> str | None:
        if self.external:
            return None
        return resolve_target(source_partname, self.target)


class Relationships:
    """The relationships belonging to one source part, in document order."""

    def __init__(self, source_partname: str, rels: list[Relationship] | None = None):
        self.source = source_partname
        self._rels: dict[str, Relationship] = {r.rid: r for r in (rels or [])}

    @classmethod
    def parse(cls, source_partname: str, blob: bytes) -> Relationships:
        root = etree.fromstring(blob)
        rels: list[Relationship] = []
        seen: set[str] = set()
        for el in root:
            # Only genuine Relationship elements. Anything else parsed as one
            # would be re-serialized AS one, changing the customer's document.
            if not isinstance(el.tag, str) or el.tag != _REL_TAG:
                continue
            rid = el.get("Id")
            if not rid:
                raise TargetError(
                    f"{source_partname or 'package root'}: relationship with no Id "
                    f"(Target={el.get('Target')!r})"
                )
            if rid in seen:
                raise TargetError(
                    f"{source_partname or 'package root'}: duplicate relationship Id {rid!r}"
                )
            seen.add(rid)
            # The XSD enumeration is case-sensitive, but files in the wild
            # spell it 'external' and Word accepts them.
            mode = (el.get("TargetMode") or "").strip().lower()
            rels.append(
                Relationship(
                    rid=rid,
                    reltype=el.get("Type", ""),
                    target=el.get("Target", ""),
                    external=mode == "external",
                )
            )
        return cls(source_partname, rels)

    def serialize(self) -> bytes:
        root = etree.Element(f"{{{NS['pr']}}}Relationships", nsmap={None: NS["pr"]})
        for rel in self._rels.values():
            attrs = {"Id": rel.rid, "Type": rel.reltype, "Target": rel.target}
            if rel.external:
                attrs["TargetMode"] = "External"
            etree.SubElement(root, f"{{{NS['pr']}}}Relationship", **attrs)
        return etree.tostring(
            root, xml_declaration=True, encoding="UTF-8", standalone=True
        )

    # -- lookup ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._rels)

    def __iter__(self):
        return iter(self._rels.values())

    def __contains__(self, rid: str) -> bool:
        return rid in self._rels

    def get(self, rid: str) -> Relationship | None:
        return self._rels.get(rid)

    def target_of(self, rid: str) -> str | None:
        rel = self._rels.get(rid)
        return rel.resolve(self.source) if rel else None

    def of_type(self, reltype: str) -> list[Relationship]:
        return [r for r in self._rels.values() if r.reltype == reltype]

    def one_of_type(self, reltype: str) -> Relationship | None:
        found = self.of_type(reltype)
        return found[0] if found else None

    def part_of_type(self, reltype: str) -> str | None:
        """Resolve the single part reached by a relationship type, if any."""
        rel = self.one_of_type(reltype)
        return rel.resolve(self.source) if rel else None

    # -- mutation -------------------------------------------------------

    def next_rid(self) -> str:
        used = {int(m.group(1)) for rid in self._rels if (m := _RID_RE.match(rid))}
        n = 1
        while n in used:
            n += 1
        return f"rId{n}"

    def add(self, reltype: str, target: str, external: bool = False) -> str:
        """Add a relationship. `target` is used verbatim; see :meth:`add_part_ref`."""
        rid = self.next_rid()
        self._rels[rid] = Relationship(rid, reltype, target, external)
        return rid

    def add_part_ref(self, reltype: str, partname: str) -> str:
        """Relate to a part by absolute part name, encoding the Target correctly."""
        return self.add(reltype, encode_target(partname, self.source))

    def put(self, rid: str, reltype: str, target: str, external: bool = False) -> str:
        """Install a relationship under a SPECIFIC rId, replacing any existing.

        Cross-package copying needs this: if a copied part keeps the rIds its
        XML already refers to, the XML needs no rewriting at all -- and not
        rewriting a customer's XML is always the safer of the two options.
        """
        self._rels[rid] = Relationship(rid, reltype, target, external)
        return rid

    def drop(self, rid: str) -> None:
        self._rels.pop(rid, None)
