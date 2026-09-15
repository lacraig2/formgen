"""Turning a package into something the aligner can line up.

The aligner works on tokens, and choosing the token is the whole game. Too
specific -- raw text -- and only boilerplate ever matches. Too loose -- the
role alone -- and every body paragraph in the corpus collapses into one
column. The token here is `(kind, key)`: a coarse kind that survives a
document saying something different, and a normalized key that pins down
*which* block when the text is shared.

Two details earn their place:

* **Heading keys drop their own numbering.** "2.3 Thermal Soak" and "Thermal
  Soak" are the same section, and a corpus where half the authors let Word
  number the headings and half typed the numbers would otherwise align to
  nothing.
* **Table cells carry their coordinates.** A cover page is usually a
  borderless layout table, and that is where the placeholders live -- the
  label cell "Report Number:" beside the value cell. Aligning on coordinates
  keeps a value cell opposite a value cell even when every document's value
  differs, which is precisely the case that matters.
"""

from __future__ import annotations

import re

from ..classify.features import DocumentContext, build_context
from ..classify.rules import EMPTY, classify_document
from ..opc.package import OpcPackage
from ..oox.walk import field_instructions, match_key
from .align import Item, Skeleton, build_skeleton

# "2.3", "2.3.", "IV.", "A)" -- Word's own numbering never reaches the text,
# but a typed one does, and the two must vote as the same section.
_ALIGNED = {"body", "table"}

_HEADING_NUMBER = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*|[IVXLC]+|[A-Za-z])[.)]?(?:\s+|\s*[-–]\s*)(?=\S)")


def heading_key(text: str) -> str:
    stripped = _HEADING_NUMBER.sub("", match_key(text), count=1)
    return (stripped or match_key(text)).lower()


def _kind(role: str, level: int | None, block) -> str:
    context = block.context
    if context.kind == "table" and context.row is not None:
        return f"cell:{context.table_depth}:{context.row}:{context.col}"
    if level is not None:
        return f"h{level}"
    return role


def document_items(pkg: OpcPackage, ctx: DocumentContext | None = None
                   ) -> tuple[list[Item], dict[int, list[Item]]]:
    """The document as a heading sequence plus the body under each heading.

    Splitting here rather than in the aligner is what keeps the second tier's
    inputs to a few dozen blocks: a section's body is aligned only against
    the same section's body in the other documents.
    """
    ctx = ctx or build_context(pkg)
    results = classify_document(ctx)
    headings: list[Item] = []
    bodies: dict[int, list[Item]] = {-1: []}
    current = -1

    for index, features in enumerate(ctx.features):
        block = features.block
        # Body paragraphs and table cells only. Headers, footers and
        # footnotes are format rather than structure -- the donor carries
        # them wholesale -- and a cover page is almost always a borderless
        # layout table, so excluding tables would exclude the placeholders.
        if not features.is_paragraph or block.context.kind not in _ALIGNED:
            continue
        result = results.get(features.path)
        role = result.role if result else "body"
        if role == EMPTY or features.is_empty:
            continue
        level = result.level if result else None
        text = features.text
        item = Item(
            kind=_kind(role, level, block),
            key=heading_key(text) if level is not None else match_key(text).lower(),
            index=index,
            text=text,
            sdt_tag=features.sdt_tag or block.context.sdt_tag or "",
            instruction=" ".join(field_instructions(block.element)),
        )
        if level is not None:
            headings.append(item)
            current = len(headings) - 1
            bodies.setdefault(current, [])
        else:
            bodies.setdefault(current, []).append(item)
    return headings, bodies


def skeleton_for(corpus: dict[str, OpcPackage],
                 contexts: dict[str, DocumentContext] | None = None) -> Skeleton:
    """Align a whole corpus. `corpus` maps document id to its package.

    `contexts` is filled in as a side effect when given, so the caller can
    reuse the donor's -- the block indices the alignment records are indices
    into *that* context's features, and rebuilding it to look them up would
    be both wasteful and a chance for the two to disagree.
    """
    headings: dict[str, list[Item]] = {}
    bodies: dict[str, dict[int, list[Item]]] = {}
    for doc in sorted(corpus):
        ctx = (contexts or {}).get(doc) or build_context(corpus[doc])
        if contexts is not None:
            contexts[doc] = ctx
        headings[doc], bodies[doc] = document_items(corpus[doc], ctx)
    return build_skeleton(headings, bodies)


def properties_for(pkg: OpcPackage) -> dict[str, str]:
    """Core and custom document properties, flattened to name -> value.

    Used to name placeholders: a field whose value is also the document's
    `Company` or a custom `ProjectNumber` is almost certainly that property,
    and the author filled in both, so the evidence is theirs rather than ours.
    """
    from ..opc.ns import RT

    out: dict[str, str] = {}
    for reltype in ("core", "custom", "extended"):
        # Document properties hang off the package root, not the
        # document part.
        part = pkg.related(RT[reltype], source="")
        if not part or part not in pkg:
            continue
        for child in pkg.element(part):
            if not isinstance(child.tag, str):
                continue
            # A custom property names itself in an attribute and holds its
            # value in a typed child; core and extended ones are plain
            # elements. Both shapes flatten to the same name -> value view.
            name = child.get("name") or child.tag.rsplit("}", 1)[-1]
            value = "".join(child.itertext()).strip()
            if value:
                out.setdefault(name, value)
    return out
