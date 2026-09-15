"""Deciding what each paragraph *is*, from evidence rather than a cascade.

A cascade ("if it has a heading style it is a heading, else if ...") fails on
exactly the documents that need this tool most. A Google-Docs export or a PDF
conversion has one style -- Normal -- on every paragraph, so the first rung of
any cascade never fires and everything below it inherits a wrong premise.

So each signal independently emits `(role, weight, evidence)` and the roles
are combined with a noisy-or: corroborating signals raise confidence without
ever exceeding 1, and a single strong signal can still win alone. The evidence
strings are kept and reported, because "we think this is a caption" is not
actionable and "we think this is a caption: it follows an image and starts
'Figure 3'" is.

Two passes follow the per-block scoring, and both matter more than they look:

* **Level assignment.** A size-based heading signal knows the paragraph is a
  heading but not which level. Levels are assigned by ranking the distinct
  heading *formats* in the document, so "the biggest heading style is level 1"
  falls out of the document's own evidence rather than an absolute size table.
* **Consistency.** Paragraphs formatted identically get the same role, and
  heading levels never skip downward by more than one. Both fix the
  single-paragraph misfires that an evidence vote will always produce
  somewhere in a 400-block document.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .features import BlockFeatures, DocumentContext

# Below this, a classification is a guess and is written to the review sidecar
# rather than acted on silently.
REVIEW_THRESHOLD = 0.55

# A style carried by this share of a document's paragraphs tells us nothing
# about any one of them.
UNINFORMATIVE_SHARE = 0.70

BODY = "body"
TITLE = "title"
CAPTION = "caption"
HEADING = "heading"            # a heading of unknown level
PLACEHOLDER = "placeholder"
LIST_BULLET = "list_bullet"
LIST_NUMBER = "list_number"
EMPTY = "empty"
TOC = "toc"
QUOTE = "quote"

# Style name -> role. Names are the cross-document key (w:styleId is localised
# for built-ins), and these are matched against the NORMALISED name.
_STYLE_ROLES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"^title$"), TITLE),
    (re.compile(r"^subtitle$"), "subtitle"),
    (re.compile(r"^heading (\d)$"), "heading{}"),
    (re.compile(r"^(caption|figure caption|table caption)$"), CAPTION),
    (re.compile(r"^toc \d$"), TOC),
    (re.compile(r"^(quote|intense quote|block text)$"), QUOTE),
    (re.compile(r"^list bullet ?\d?$"), LIST_BULLET),
    (re.compile(r"^list (number|paragraph) ?\d?$"), LIST_NUMBER),
    (re.compile(r"^(body text|normal|default paragraph|plain text|"
                r"body text \d|no spacing)$"), BODY),
)


def role_for_style_name(name: str) -> str | None:
    for pattern, role in _STYLE_ROLES:
        match = pattern.match(name)
        if match:
            return role.format(*match.groups()) if match.groups() else role
    return None


@dataclass(frozen=True)
class Signal:
    role: str
    weight: float
    evidence: str
    # A prior is a weak default rather than evidence: "this paragraph carries
    # the style 97% of the document carries". Priors do not accumulate with
    # each other or with real evidence -- two uninformative observations are
    # not one informative one -- so they act as a floor instead.
    prior: bool = False


@dataclass
class Classification:
    path: str
    role: str
    confidence: float
    evidence: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, float], ...] = ()
    level: int | None = None

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_THRESHOLD


def _noisy_or(weights: Iterable[float]) -> float:
    """Combine independent evidence without ever exceeding certainty."""
    product = 1.0
    for weight in weights:
        product *= (1.0 - max(0.0, min(1.0, weight)))
    return 1.0 - product


# -- the signals ----------------------------------------------------------


def signals_for(
    features: BlockFeatures,
    ctx: DocumentContext,
    known_styles: set[str] | None = None,
    placeholders: set[str] | None = None,
) -> list[Signal]:
    out: list[Signal] = []
    if not features.is_paragraph:
        return out
    if features.is_empty:
        return [Signal(EMPTY, 0.99, "no text")]

    known_styles = known_styles or set()
    placeholders = placeholders or set()

    tag = features.sdt_tag or ""
    name = tag.split(".", 1)[1] if tag.startswith("formgen.") else tag
    if name and name in placeholders:
        # Authoritative: the document itself declares this is the field.
        return [Signal(PLACEHOLDER, 0.99, f"content control tagged {tag!r}")]

    # An explicit w:numPr is the document declaring "this is item N of a
    # list" -- a structural fact, not an appearance. It therefore outranks
    # every appearance signal, and it suppresses the two that would otherwise
    # argue for body text: a list item is body-sized and usually carries the
    # default style, so neither observation discriminates at all.
    numbered = bool(features.numbering and features.numbering.num_id)

    role = role_for_style_name(features.style_name)
    if role == BODY and numbered:
        role = None
    if role:
        share = ctx.style_share.get(features.style_name, 0.0)
        if share >= UNINFORMATIVE_SHARE:
            # Every paragraph is this style, so being this style distinguishes
            # nothing. This is the converted-document case -- Google Docs and
            # PDF exports put Normal on the lot -- and letting the style win
            # here classifies the entire document as body text, title
            # included.
            out.append(Signal(role, 0.35,
                              f"styled {features.style_name!r}, but so is "
                              f"{share:.0%} of the document",
                              prior=True))
        elif features.style_name in known_styles:
            out.append(Signal(role, 0.95,
                              f"styled {features.style_name!r}, a profile style"))
        else:
            out.append(Signal(role, 0.85,
                              f"styled {features.style_name!r} (not in the profile)"))

    if features.outline_level is not None:
        out.append(Signal(
            f"heading{features.outline_level + 1}", 0.90,
            f"outline level {features.outline_level} resolved through the style",
        ))

    if numbered:
        listed = LIST_BULLET if features.numbering.is_bullet else LIST_NUMBER
        out.append(Signal(
            listed, 0.95,
            f"numbered by list {features.numbering.num_id} "
            f"at level {features.numbering.ilvl}",
        ))

    if features.seq_kind in ("Figure", "Table"):
        out.append(Signal(CAPTION, 0.92,
                          f"contains a SEQ {features.seq_kind} field"))
    elif features.caption_lead and (features.follows_graphic or features.precedes_graphic):
        where = "follows" if features.follows_graphic else "precedes"
        out.append(Signal(CAPTION, 0.70,
                          f"starts like a caption and {where} a figure or table"))

    if not numbered:
        out.extend(_size_signals(features, ctx))
    if (
        features.run.bold
        and 0 < features.words < 12
        and not features.ends_with_terminal
        and features.para.keep_next
    ):
        out.append(Signal(HEADING, 0.60,
                          "bold, short, unpunctuated and keeps with the next"))
    if features.all_caps and 0 < features.words < 12 and not features.ends_with_terminal:
        out.append(Signal(HEADING, 0.45, "short all-capitals line"))
    if not any(not signal.prior for signal in out):
        # Only priors fired, which is the same as nothing firing: a paragraph
        # with no distinguishing feature in a document where its style tells
        # us nothing is body text by default, and saying so beats leaving the
        # role to a 0.35 prior nobody can act on.
        out.append(Signal(BODY, 0.50, "nothing distinguishes it from body text"))
    return out


def _size_signals(features: BlockFeatures, ctx: DocumentContext) -> list[Signal]:
    """Relative size, always against THIS document's own body text."""
    ratio = features.size_ratio
    if ratio is None:
        return []
    if ratio >= 1.8 and features.words < 25:
        # Size alone cannot distinguish a title from a big heading. What can
        # is position: a title opens the document. A 20pt heading halfway
        # down is a heading, and calling it a title would give the document
        # two of them.
        if features.body_ordinal is not None and features.body_ordinal <= 3:
            return [Signal(TITLE, 0.65, f"{ratio:.1f}x the body text size, "
                                        "at the top of the document")]
        return [Signal(HEADING, 0.65, f"{ratio:.1f}x the body text size")]
    if ratio >= 1.12 and features.words < 30 and not features.ends_with_terminal:
        return [Signal(HEADING, 0.65, f"{ratio:.1f}x the body text size")]
    if ratio <= 0.92 and (features.follows_graphic or features.precedes_graphic):
        return [Signal(CAPTION, 0.55,
                       f"{ratio:.1f}x the body size, next to a figure or table")]
    if 0.93 <= ratio <= 1.07:
        # Looking exactly like the prose around it is positive evidence for
        # being prose, not an absence of evidence -- and a document is mostly
        # body text, so this signal fires more than any other. The upper
        # bound is tight on purpose: a heading only 10% larger than the body
        # is common, and a wider band swallows it.
        return [Signal(BODY, 0.60, "the same size as the body text")]
    return []


# -- scoring --------------------------------------------------------------


def classify_block(
    features: BlockFeatures,
    ctx: DocumentContext,
    known_styles: set[str] | None = None,
    placeholders: set[str] | None = None,
) -> Classification:
    signals = signals_for(features, ctx, known_styles, placeholders)
    if not signals:
        return Classification(features.path, BODY, 0.0, ("no signals",))

    by_role: dict[str, list[Signal]] = defaultdict(list)
    for signal in signals:
        by_role[signal.role].append(signal)
    scored = {}
    for role, group in by_role.items():
        evidence = [s.weight for s in group if not s.prior]
        priors = [s.weight for s in group if s.prior]
        scored[role] = max(
            _noisy_or(evidence) if evidence else 0.0,
            max(priors, default=0.0),
        )
    # A generic heading signal corroborates a levelled one rather than
    # competing with it: both say "heading", and only one of them knows which.
    levelled = [r for r in scored if r.startswith("heading") and r != HEADING]
    if HEADING in scored and levelled:
        best = max(levelled, key=lambda r: scored[r])
        scored[best] = _noisy_or([scored[best], scored.pop(HEADING)])

    ranked = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))
    role, confidence = ranked[0]
    return Classification(
        path=features.path,
        role=role,
        confidence=confidence,
        evidence=tuple(s.evidence for s in by_role.get(role, []) or signals),
        alternatives=tuple((r, round(c, 3)) for r, c in ranked[1:4]),
        level=_level_of(role),
    )


def _level_of(role: str) -> int | None:
    if role.startswith("heading") and role != HEADING:
        try:
            return int(role[len("heading"):])
        except ValueError:
            return None
    return None


# -- document-level passes ------------------------------------------------


def classify_document(
    ctx: DocumentContext,
    known_styles: set[str] | None = None,
    placeholders: set[str] | None = None,
) -> dict[str, Classification]:
    results = {
        f.path: classify_block(f, ctx, known_styles, placeholders)
        for f in ctx.features
        if f.is_paragraph
    }
    _one_title(ctx, results)
    _assign_levels(ctx, results)
    _enforce_consistency(ctx, results)
    _repair_level_skips(ctx, results)
    return results


def _one_title(ctx: DocumentContext, results: dict[str, Classification]) -> None:
    """A document has at most one title; later ones are headings.

    Keeping the first rather than the most confident matters: a title is
    defined by where it is, and a runner-up further down the document is a
    section heading whatever its size.
    """
    titles = [f.path for f in ctx.features
              if f.is_paragraph and results.get(f.path)
              and results[f.path].role == TITLE]
    for path in titles[1:]:
        current = results[path]
        results[path] = Classification(
            path=path, role=HEADING, confidence=current.confidence,
            evidence=current.evidence + ("the document already has a title",),
            alternatives=current.alternatives, level=None,
        )


def _assign_levels(ctx: DocumentContext, results: dict[str, Classification]) -> None:
    """Give unlevelled headings a level, by ranking heading FORMATS.

    Size alone says "this is a heading"; it cannot say which level. Ranking
    the distinct heading formats the document actually uses -- biggest first --
    derives the hierarchy from the document's own evidence, which is the only
    thing available when nothing is styled.
    """
    unlevelled = [p for p, c in results.items() if c.role == HEADING]
    if not unlevelled:
        return
    formats: dict[tuple, list[str]] = defaultdict(list)
    for path in unlevelled:
        feature = ctx.by_path(path)
        if feature is None:
            continue
        formats[(
            -(feature.run.size.half_points if feature.run.size else 0),
            not bool(feature.run.bold),
            feature.para.indent_left.twips if feature.para.indent_left else 0,
        )].append(path)

    # Levels start below whatever levelled headings already exist, so an
    # inferred heading never claims to outrank a styled one.
    existing = {c.level for c in results.values() if c.level}
    base = 1 if not existing else max(1, min(existing))
    for offset, key in enumerate(sorted(formats)):
        level = min(9, base + offset)
        for path in formats[key]:
            classification = results[path]
            results[path] = Classification(
                path=path, role=f"heading{level}", confidence=classification.confidence,
                evidence=classification.evidence + (
                    f"heading format #{offset + 1} of {len(formats)} by size",
                ),
                alternatives=classification.alternatives, level=level,
            )


def _enforce_consistency(ctx: DocumentContext, results: dict[str, Classification]) -> None:
    """Paragraphs formatted identically get the same role.

    One paragraph in twenty will lose a close vote for an accidental reason --
    it happened to end without a full stop, or to sit next to an image. Making
    a format signature decide once, by weight of evidence across the document,
    removes almost all of those.
    """
    groups: dict[tuple, list[str]] = defaultdict(list)
    for feature in ctx.features:
        if feature.is_paragraph and not feature.is_empty:
            groups[feature.signature].append(feature.path)
    for paths in groups.values():
        if len(paths) < 3:
            # Too few to be a pattern; a lone paragraph is allowed to differ.
            continue
        votes: dict[str, float] = defaultdict(float)
        for path in paths:
            votes[results[path].role] += results[path].confidence
        winner = max(votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
        for path in paths:
            current = results[path]
            if current.role == winner:
                continue
            results[path] = Classification(
                path=path, role=winner,
                confidence=min(0.95, votes[winner] / len(paths)),
                evidence=current.evidence + (
                    f"{len(paths)} paragraphs share this exact format and "
                    f"{winner} won across them",
                ),
                alternatives=current.alternatives, level=_level_of(winner),
            )


def _repair_level_skips(ctx: DocumentContext, results: dict[str, Classification]) -> None:
    """Heading levels may not jump down by more than one.

    A document that goes 1 -> 3 has either a missing level 2 or a
    misclassified paragraph; either way the outline is wrong, and pulling the
    level up keeps the heading path in the locator honest.
    """
    previous = 0
    for feature in ctx.features:
        if not feature.is_paragraph:
            continue
        classification = results.get(feature.path)
        if classification is None or classification.level is None:
            continue
        level = classification.level
        if level > previous + 1 and previous > 0:
            fixed = previous + 1
            results[feature.path] = Classification(
                path=feature.path, role=f"heading{fixed}",
                confidence=classification.confidence * 0.9,
                evidence=classification.evidence + (
                    f"level {level} would skip down from {previous}; "
                    f"promoted to {fixed}",
                ),
                alternatives=classification.alternatives, level=fixed,
            )
            level = fixed
        previous = level


def roles_of(results: dict[str, Classification]) -> dict[str, str]:
    return {path: c.role for path, c in results.items()}


def review_needed(results: dict[str, Classification]) -> list[Classification]:
    return sorted(
        (c for c in results.values() if c.needs_review),
        key=lambda c: c.path,
    )
