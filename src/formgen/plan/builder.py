"""Building the plan: the one engine behind lint, apply and dry-run.

`lint` renders what this returns. `apply` executes it. Because both read the
same `Plan`, "the linter reports exactly what the reformatter would change" is
structural rather than aspirational.

Two reporting decisions do most of the work of making the output usable:

**Deviations are aggregated, not enumerated.** A document whose body text is
12pt does not have 180 problems; it has one problem in 180 places. Findings
are grouped by (role, property, wrong value) and reported once with a count
and one find string, so the report stays the length of the format rather than
the length of the document.

**Severity comes from the corpus, not from us.** A property the exemplars
agreed on unanimously is an error; one they were split on is a warning; one
too rare to have an opinion about does not fire at all. That mapping is
computed during `learn` and simply read here, which is why lint on a learned
profile is quiet enough to be worth running.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..analyze.stats import bucket
from ..classify.features import DocumentContext, build_context
from ..classify.rules import (
    EMPTY, LIST_BULLET, LIST_NUMBER, PLACEHOLDER, Classification, classify_document, role_for_style_name,
)
from ..learn.skeleton import heading_key
from ..oox.styles import normalize_style_name
from ..oox.walk import Block, exact_key, match_key
from ..opc.package import OpcPackage
from ..profile.io import Profile
from ..profile.schema import encode
from .locator import LocatorFactory, document_locator
from .model import (
    ERROR, INFO, WARN, Edit, Finding, Plan, SetPStyle, StripPPr, StripRPr,
)

# Run and paragraph properties worth reporting per role, and the rule code
# each one reports under.
RUN_RULES = {
    "size": "style.size",
    "font_ascii": "style.font",
    "bold": "style.bold",
    "italic": "style.italic",
    "caps": "style.caps",
    "color": "style.color",
}
PARA_RULES = {
    "alignment": "style.alignment",
    "space_before": "style.space_before",
    "space_after": "style.space_after",
    "line_spacing": "style.line_spacing",
    "indent_left": "style.indent_left",
    "indent_first_line": "style.indent_first_line",
}
PAGE_RULES = {
    "/page/primary/orientation": ("page.orientation", "page orientation"),
    "/page/primary/width": ("page.size", "page width"),
    "/page/primary/height": ("page.size", "page height"),
    "/page/primary/margins/top": ("page.margins", "top margin"),
    "/page/primary/margins/bottom": ("page.margins", "bottom margin"),
    "/page/primary/margins/left": ("page.margins", "left margin"),
    "/page/primary/margins/right": ("page.margins", "right margin"),
    "/page/primary/margins/header": ("page.margins", "header distance"),
    "/page/primary/margins/footer": ("page.margins", "footer distance"),
    "/page/primary/columns": ("page.columns", "column count"),
}


@dataclass
class _Deviation:
    """One (role, property, wrong value) group, with every place it occurs."""

    code: str
    rule_id: str
    severity: str
    role: str
    property: str
    expected: Any
    actual: Any
    blocks: list[Block] = field(default_factory=list)


def build_plan(
    pkg: OpcPackage,
    profile: Profile,
    document_name: str = "",
    pages: dict[str, int] | None = None,
) -> Plan:
    """Everything wrong with `pkg`, and what would put it right."""
    plan = Plan(document=document_name, profile=profile.name)
    ctx = build_context(pkg)

    plan.refusals = _refusals(ctx, pkg)
    if plan.refusals:
        for reason in plan.refusals:
            plan.add(Finding(
                code="document.refused", severity=ERROR, message=reason,
                locator=document_locator(pkg.main_document), fixable=False,
            ))
        return plan

    classifications = classify_document(
        ctx, profile.style_names, set(profile.placeholders)
    )
    locators = LocatorFactory(
        ctx.blocks, {p: c.role for p, c in classifications.items()}, pages or {}
    )
    role_styles = _role_to_style(profile)

    _check_page_setup(plan, ctx, profile, pkg)
    _check_styles(plan, ctx, profile, classifications, locators, role_styles)
    _check_lists(plan, ctx, profile, classifications, locators)
    _check_headers(plan, ctx, profile, pkg)
    _check_placeholders(plan, ctx, profile, classifications, locators)
    _check_structure(plan, ctx, profile, classifications, locators)
    _build_edits(plan, ctx, classifications, locators, role_styles)

    plan.stats = {
        "blocks": len(ctx.blocks),
        "paragraphs": sum(1 for f in ctx.features if f.is_paragraph),
        "modal_body_size": str(ctx.modal_size) if ctx.modal_size else None,
        "roles": _role_counts(classifications),
        "needs_review": len(plan.needs_review),
    }
    return plan


def _refusals(ctx: DocumentContext, pkg: OpcPackage) -> list[str]:
    """What we will not touch, each naming its remedy.

    Refusing to restyle a document with revision records is not caution: the
    records say who changed what, and restyling makes every one of them a
    lie about formatting nobody chose.
    """
    reasons = list(ctx.settings.refusal_reasons)
    if any(f.block.kind == "altChunk" for f in ctx.features):
        reasons.append(
            "this document imports content with w:altChunk, which Word renders "
            "from a part we cannot inspect or restyle. Open it in Word and "
            "save a copy -- Word flattens the import -- then run against that."
        )
    if not ctx.settings.track_changes and _has_revisions(ctx):
        reasons.append(
            "the document contains tracked changes. Restyling would rewrite "
            "formatting inside revision records. Accept or reject them "
            "(Review > Accept > All Changes) and re-run."
        )
    return reasons


def _has_revisions(ctx: DocumentContext) -> bool:
    return any(f.block.context.in_del or f.block.context.in_ins for f in ctx.features)


def _role_to_style(profile: Profile) -> dict[str, str]:
    """role -> the profile style name that implements it.

    Styles the corpus used come first, because a style a real document
    carried is the real article. Roles learned from appearance fill in the
    rest: on a corpus with no styles that is the *only* mapping there is, and
    `learn` has written a style into the donor for each of them, so there is
    something for `w:pStyle` to point at.
    """
    out: dict[str, str] = {}
    for name in sorted(profile.style_names):
        role = role_for_style_name(name)
        if role:
            out.setdefault(role, name)
    for role, name in sorted(profile.role_styles.items()):
        out.setdefault(role, normalize_style_name(name))
    return out


def _role_counts(classifications: dict[str, Classification]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for classification in classifications.values():
        counts[classification.role] += 1
    return dict(sorted(counts.items()))


# -- page setup -----------------------------------------------------------


def _check_page_setup(plan: Plan, ctx: DocumentContext, profile: Profile,
                      pkg: OpcPackage) -> None:
    from ..learn.observe import _observe_sections, _primary_section, DocObservations

    observed = DocObservations(doc="")
    _observe_sections(observed, ctx.sections, _primary_section(pkg, ctx.sections))
    values = observed.single()
    locator = document_locator(pkg.main_document)

    for pointer, (code, label) in PAGE_RULES.items():
        severity = profile.severity(pointer)
        if severity == "off":
            continue
        expected = profile.value(pointer)
        actual = values.get(pointer)
        if expected is None or actual is None:
            continue
        name = pointer.rsplit("/", 1)[-1]
        if bucket(actual, name) == bucket(expected, name):
            continue
        plan.add(Finding(
            code=code, severity=severity, rule_id=pointer,
            message=(f"{label} is {_shown(actual, pointer)}; "
                     f"the profile says {_shown(expected, pointer)}"),
            locator=locator, expected=expected, actual=actual, fixable=True,
        ))

    for problem in ctx.sections.problems():
        plan.add(Finding(
            code="page.structure", severity=WARN, message=problem,
            locator=locator, fixable=False,
        ))


# -- styles ---------------------------------------------------------------


def _check_styles(plan: Plan, ctx: DocumentContext, profile: Profile,
                  classifications: dict[str, Classification],
                  locators: LocatorFactory, role_styles: dict[str, str]) -> None:
    groups: dict[tuple, _Deviation] = {}

    for feature in ctx.features:
        if not feature.is_paragraph:
            continue
        classification = classifications.get(feature.path)
        if classification is None or classification.role in (EMPTY, PLACEHOLDER):
            continue
        style_name = role_styles.get(classification.role)
        if style_name is None:
            continue

        if feature.style_name != style_name:
            _add(groups, _Deviation(
                code="style.wrong", rule_id=f"/styles/paragraph/{style_name}",
                severity=WARN if classification.needs_review else ERROR,
                role=classification.role, property="style",
                expected=style_name, actual=feature.style_name or "(none)",
            ), feature.block)
            # Every property of a wrongly-styled paragraph will also differ.
            # Reporting those too turns one problem into six and buries the
            # one finding that actually fixes it.
            continue

        base = f"/styles/paragraph/{style_name}"
        for prop, code in RUN_RULES.items():
            _compare(groups, profile, f"{base}/run/{prop}", code,
                     classification.role, prop,
                     getattr(feature.run, prop, None), feature.block)
        for prop, code in PARA_RULES.items():
            _compare(groups, profile, f"{base}/para/{prop}", code,
                     classification.role, prop,
                     getattr(feature.para, prop, None), feature.block)

    for deviation in groups.values():
        plan.add(_finding_for(deviation, locators))


def _compare(groups: dict, profile: Profile, pointer: str, code: str,
             role: str, prop: str, actual: Any, block: Block) -> None:
    severity = profile.severity(pointer)
    if severity == "off":
        return
    expected = profile.value(pointer)
    if expected is None:
        return
    if actual is None:
        # "error-if-present" is the severity for an optional property: it is
        # policed when it is set, and its absence is not a deviation.
        if profile.status(pointer) == "optional_consistent":
            return
    name = pointer.rsplit("/", 1)[-1]
    if bucket(actual, name) == bucket(expected, name):
        return
    _add(groups, _Deviation(
        code=code, rule_id=pointer, severity=severity, role=role,
        property=prop, expected=expected, actual=actual,
    ), block)


def _add(groups: dict, deviation: _Deviation, block: Block) -> None:
    key = (deviation.code, deviation.role, deviation.property,
           repr(bucket(deviation.actual, deviation.property)))
    existing = groups.get(key)
    if existing is None:
        deviation.blocks.append(block)
        groups[key] = deviation
    else:
        existing.blocks.append(block)


def _shown(value: Any, pointer: str) -> str:
    """A value as it appears in a message. None is "not set", never "None"."""
    return "not set" if value is None else str(encode(value, pointer))


def _finding_for(deviation: _Deviation, locators: LocatorFactory) -> Finding:
    count = len(deviation.blocks)
    where = f" in {count} paragraphs" if count > 1 else ""
    if deviation.code == "style.wrong":
        message = (
            f"{count} paragraph(s) we read as {deviation.role} are styled "
            f"{deviation.actual!r}; the profile's style is {deviation.expected!r}"
        )
    else:
        message = (
            f"{deviation.role} {deviation.property.replace('_', ' ')} is "
            f"{_shown(deviation.actual, deviation.rule_id)}{where}; "
            f"the profile says {_shown(deviation.expected, deviation.rule_id)}"
        )
    return Finding(
        code=deviation.code, severity=deviation.severity, message=message,
        locator=locators.of(deviation.blocks[0]), rule_id=deviation.rule_id,
        expected=deviation.expected, actual=deviation.actual, fixable=True,
    )


# -- lists ----------------------------------------------------------------


def _check_lists(plan: Plan, ctx: DocumentContext, profile: Profile,
                 classifications: dict[str, Classification],
                 locators: LocatorFactory) -> None:
    seen: set[tuple[str, int, str]] = set()
    for feature in ctx.features:
        classification = classifications.get(feature.path)
        if classification is None or classification.role not in (LIST_BULLET, LIST_NUMBER):
            continue
        reference = feature.numbering
        if reference is None or reference.level is None:
            continue
        kind = "bullet" if reference.is_bullet else "numbered"
        level = reference.ilvl
        for prop, actual in (
            ("marker", reference.level.marker),
            ("indent_left", reference.level.indent_left),
            ("indent_hanging", reference.level.indent_hanging),
        ):
            pointer = f"/lists/{kind}/level{level}/{prop}"
            severity = profile.severity(pointer)
            expected = profile.value(pointer)
            if severity == "off" or expected is None or actual is None:
                continue
            if bucket(actual, prop) == bucket(expected, prop):
                continue
            key = (kind, level, prop)
            if key in seen:
                continue
            seen.add(key)
            plan.add(Finding(
                code=f"list.{prop}", severity=severity, rule_id=pointer,
                message=(
                    f"the {kind} list at level {level} has "
                    f"{prop.replace('_', ' ')} {_shown(actual, pointer)}; "
                    f"the profile says {_shown(expected, pointer)}"
                ),
                locator=locators.of(feature.block),
                expected=expected, actual=actual, fixable=True,
            ))


# -- headers and footers --------------------------------------------------


def _check_headers(plan: Plan, ctx: DocumentContext, profile: Profile,
                   pkg: OpcPackage) -> None:
    from ..learn.observe import DocObservations, _observe_hdrftr

    observed = DocObservations(doc="")
    _observe_hdrftr(observed, pkg, ctx.sections)
    values = observed.single()
    locator = document_locator(pkg.main_document)

    for pointer in profile.pointers_under("/hdrftr"):
        if not pointer.endswith("/text"):
            continue
        severity = profile.severity(pointer)
        if severity == "off":
            continue
        expected = profile.value(pointer)
        actual = values.get(pointer)
        _, _, kind, slot, _ = pointer.split("/")
        if actual is None:
            plan.add(Finding(
                code="hdrftr.missing", severity=severity, rule_id=pointer,
                message=(
                    f"no {slot} {kind} -- note that an absent header "
                    "reference means 'same as the previous section', not "
                    "'no header'; suppressing one needs an explicitly empty "
                    "header part"
                ),
                locator=locator, expected=expected, actual=None, fixable=True,
            ))
        elif match_key(str(actual)) != match_key(str(expected)):
            plan.add(Finding(
                code="hdrftr.text", severity=severity, rule_id=pointer,
                message=(f"the {slot} {kind} reads {actual!r}; "
                         f"the profile says {expected!r}"),
                locator=locator, expected=expected, actual=actual, fixable=True,
            ))


# -- placeholders ---------------------------------------------------------


def _check_placeholders(plan: Plan, ctx: DocumentContext, profile: Profile,
                        classifications: dict[str, Classification],
                        locators: LocatorFactory) -> None:
    required = profile.required_placeholders()
    if not required:
        return
    filled, empty = _placeholder_values(ctx, profile)

    for name, feature in sorted(empty.items()):
        if name in filled:
            continue
        plan.add(Finding(
            code="placeholder.empty", severity=WARN,
            rule_id=f"/placeholders/{name}",
            message=f"the {name!r} field is empty",
            locator=locators.of(feature.block), fixable=False,
        ))
    frames = _frames(profile)
    has_controls = any(
        (f.sdt_tag or "").startswith("formgen.") for f in ctx.features
    )
    # Only *inferred* placeholders can be written off as unverifiable. One a
    # user declared by hand in overrides.yaml is a requirement they stated,
    # and its absence is an error whatever the document looks like.
    inferred = {s.get("name") for s in profile.slots("placeholder")}
    unverifiable = []
    for name in sorted(required - set(filled) - set(empty)):
        if not has_controls and name not in frames and name in inferred:
            # No control anywhere and no literal wording around the field:
            # there is nothing in the document to look for. Saying "missing"
            # would be a guess dressed as a finding.
            unverifiable.append(name)
            continue
        plan.add(Finding(
            code="placeholder.missing", severity=ERROR,
            rule_id=f"/placeholders/{name}",
            message=(f"the document has no {name!r} field; the profile's "
                     "template carries a content control for it"),
            locator=document_locator(), fixable=False,
        ))
    if unverifiable:
        plan.add(Finding(
            code="placeholder.unverifiable", severity=INFO,
            rule_id="/placeholders",
            message=(
                f"{len(unverifiable)} field(s) cannot be checked in a document "
                f"with no content controls: {', '.join(unverifiable)}. "
                "Documents made with `formgen new`, or from template.docx, "
                "carry them."
            ),
            locator=document_locator(), fixable=False,
        ))


def _placeholder_values(ctx: DocumentContext, profile: Profile
                        ) -> tuple[dict[str, tuple[str, Any]], dict[str, Any]]:
    """Find each placeholder's value, by control *or* by its literal frame.

    A foreign document has no content controls at all -- that is the normal
    case for `lint`, and reporting every learned field as missing would make
    the rule useless on exactly the documents it exists for. So a field is
    also found by the boilerplate around it: the corpus said this paragraph
    reads "Report No. " and then something, so a paragraph that reads
    "Report No. LR-2027-0001" has the field, control or no control.
    """
    filled: dict[str, tuple[str, Any]] = {}
    empty: dict[str, Any] = {}

    for feature in ctx.features:
        tag = feature.sdt_tag or ""
        if not tag.startswith("formgen."):
            continue
        name = tag.split(".", 1)[1]
        value = feature.text.strip()
        if value and not feature.block.context.is_placeholder:
            filled.setdefault(name, (value, feature))
        else:
            empty.setdefault(name, feature)

    frames = _frames(profile)
    if not frames:
        return filled, empty
    for feature in ctx.features:
        if not feature.is_paragraph:
            continue
        text = match_key(feature.text)
        for name, (prefix, suffix) in frames.items():
            if name in filled or not text.startswith(prefix):
                continue
            if suffix and not text.endswith(suffix):
                continue
            value = text[len(prefix):len(text) - len(suffix)].strip()
            if value:
                filled[name] = (value, feature)
            else:
                empty.setdefault(name, feature)
    return filled, empty


def _frames(profile: Profile) -> dict[str, tuple[str, str]]:
    """name -> (literal before the value, literal after it), match-normalized.

    Only fields with a literal on at least one side are usable this way. A
    placeholder that is a whole paragraph of its own -- an author line, say --
    has no frame to recognize, and guessing at one would match every
    paragraph in the document.
    """
    out: dict[str, tuple[str, str]] = {}
    for slot in profile.slots("placeholder"):
        name = slot.get("name")
        template = slot.get("template") or ""
        marker = "{" + str(name) + "}"
        if not name or marker not in template:
            continue
        before, after = template.split(marker, 1)
        prefix, suffix = match_key(before), match_key(after)
        if prefix or suffix:
            out[name] = (prefix, suffix)
    return out


# -- structure ------------------------------------------------------------


def _check_structure(plan: Plan, ctx: DocumentContext, profile: Profile,
                     classifications: dict[str, Classification],
                     locators: LocatorFactory) -> None:
    """Required sections, in order, and boilerplate word for word.

    Only the confident part of the skeleton is enforced. A section present in
    60% of the exemplars is as likely to be optional as forgotten, and a rule
    built on that guess fires on documents that are perfectly fine -- which
    is how a linter teaches people to ignore it.
    """
    if not profile.skeleton:
        return
    _check_sections(plan, ctx, profile, classifications, locators)
    _check_boilerplate(plan, ctx, profile, locators)
    _check_patterns(plan, ctx, profile, locators)


def _check_sections(plan: Plan, ctx: DocumentContext, profile: Profile,
                    classifications: dict[str, Classification],
                    locators: LocatorFactory) -> None:
    required = profile.required_sections()
    if not required:
        return
    found: list[tuple[str, Block]] = []
    for feature in ctx.features:
        result = classifications.get(feature.path)
        if result is None or result.level is None:
            continue
        found.append((heading_key(feature.text), feature.block))
    present = {key for key, _ in found}

    for slot in required:
        key = heading_key(slot.get("section") or slot.get("text") or "")
        if key and key not in present:
            plan.add(Finding(
                code="structure.section_missing",
                severity=_structure_severity(profile, "structure.section_missing"),
                rule_id=f"/skeleton/slots/{slot['index']}",
                message=(f"the document has no {slot.get('section')!r} section; "
                         "every exemplar has one"),
                locator=document_locator(), fixable=False,
            ))

    # Order is checked only over the sections the document actually has:
    # reporting a missing section again as "out of order" is noise.
    wanted = [heading_key(s.get("section") or "") for s in required]
    wanted = [k for k in wanted if k in present]
    actual = [key for key, _ in found if key in set(wanted)]
    seen: list[str] = []
    for key in actual:
        if key not in seen:
            seen.append(key)
    if seen != wanted:
        plan.add(Finding(
            code="structure.section_order",
            severity=_structure_severity(profile, "structure.section_order"),
            rule_id="/skeleton/order",
            message=(
                "sections are in a different order than the exemplars: "
                f"expected {' > '.join(wanted)}, found {' > '.join(seen)}"
            ),
            locator=document_locator(), fixable=False,
        ))


def _check_boilerplate(plan: Plan, ctx: DocumentContext, profile: Profile,
                       locators: LocatorFactory) -> None:
    """Fixed wording, located tolerantly and then verified strictly.

    The two keys are deliberately different. `match_key` folds dashes, quotes
    and ligatures so the paragraph is *found* despite cosmetic differences;
    `exact_key` then keeps those characters distinct so the report can say a
    distribution statement has the wrong dash rather than quietly passing it.
    """
    by_match: dict[str, list] = defaultdict(list)
    for feature in ctx.features:
        if feature.is_paragraph and feature.text.strip():
            by_match[match_key(feature.text)].append(feature)

    for slot in profile.slots("boilerplate"):
        # `learn` decided this, so that what it printed and what lint
        # enforces cannot drift apart.
        if not slot.get("enforced"):
            continue
        wanted = slot.get("text") or ""
        if not wanted.strip():
            continue
        candidates = by_match.get(match_key(wanted), [])
        if not candidates:
            # A near miss is far more useful than "missing": the author did
            # write the passage and changed a word, and pointing at the
            # paragraph they changed is the whole job.
            near = _nearest(wanted, by_match)
            if near is None:
                plan.add(Finding(
                    code="structure.boilerplate_missing",
                    severity=_structure_severity(profile, "structure.boilerplate_missing"),
                    rule_id=f"/skeleton/slots/{slot['index']}",
                    message=f"required wording is missing: {_clip(wanted)}",
                    expected=wanted, locator=document_locator(), fixable=False,
                ))
                continue
            candidates = [near]
        exact = exact_key(wanted)
        if any(exact_key(f.text) == exact for f in candidates):
            continue
        feature = candidates[0]
        plan.add(Finding(
            code="structure.boilerplate_altered",
            severity=_structure_severity(profile, "structure.boilerplate_altered"),
            rule_id=f"/skeleton/slots/{slot['index']}",
            message=(f"required wording differs from the profile: "
                     f"{_clip(feature.text)}"),
            expected=wanted, actual=feature.text,
            locator=locators.of(feature.block), fixable=False,
        ))


def _check_patterns(plan: Plan, ctx: DocumentContext, profile: Profile,
                    locators: LocatorFactory) -> None:
    """A filled field whose value does not look like the others.

    The pattern came from a handful of examples, so this is a warning and
    never an error, and `overrides.yaml` is where a user loosens or deletes
    it once they have seen the first false positive.
    """
    import re as _re

    patterns = {
        name: body["pattern"]
        for name, body in profile.placeholders.items()
        if body.get("pattern")
    }
    if not patterns:
        return
    filled, _ = _placeholder_values(ctx, profile)
    for name, (value, feature) in sorted(filled.items()):
        pattern = patterns.get(name)
        if not pattern:
            continue
        try:
            matches = _re.search(pattern, value) is not None
        except _re.error:
            plan.add(Finding(
                code="placeholder.bad_pattern", severity=WARN,
                rule_id=f"/placeholders/{name}",
                message=(f"the pattern for {name!r} in overrides.yaml is not a "
                         f"valid regular expression: {pattern}"),
                locator=document_locator(), fixable=False,
            ))
            patterns[name] = ""
            continue
        if not matches:
            plan.add(Finding(
                code="placeholder.pattern", severity=WARN,
                rule_id=f"/placeholders/{name}",
                message=(f"{name} is {value!r}, which does not match the shape "
                         f"the exemplars share ({pattern})"),
                expected=pattern, actual=value,
                locator=locators.of(feature.block), fixable=False,
            ))


NEAR_MISS = 0.80


def _nearest(wanted: str, by_match: dict[str, list]):
    """The closest paragraph in the document, if it is close enough."""
    from difflib import SequenceMatcher

    target = match_key(wanted)
    best, score = None, NEAR_MISS
    for key, features in sorted(by_match.items()):
        ratio = SequenceMatcher(None, target, key, autojunk=False).ratio()
        if ratio > score:
            best, score = features[0], ratio
    return best


def _structure_severity(profile: Profile, code: str) -> str:
    """Structural rules are severity-tunable by code, not by pointer.

    They are not learned properties with a coverage and an agreement, so
    there is no vote to read a severity off; `overrides.yaml` names the rule
    family directly.
    """
    override = profile.overrides.severity.get(code)
    if override:
        return override
    # Missing wording is a warning because no command adds it: `apply`
    # reformats what is there and cannot invent a distribution statement, so
    # an error here would be a permanent one. Wording that IS there and has
    # been altered is an error -- somebody edited it.
    if code in ("structure.section_order", "structure.boilerplate_missing",
                "structure.section_missing"):
        return WARN
    return ERROR


def _clip(text: str, width: int = 60) -> str:
    text = " ".join(text.split())
    return repr(text if len(text) <= width else text[:width - 3] + "...")


# -- edits ----------------------------------------------------------------


def _build_edits(plan: Plan, ctx: DocumentContext,
                 classifications: dict[str, Classification],
                 locators: LocatorFactory, role_styles: dict[str, str]) -> None:
    """What `apply` would do. Described here, executed in Phase 3."""
    for feature in ctx.features:
        classification = classifications.get(feature.path)
        if classification is None or classification.role in (EMPTY, PLACEHOLDER):
            continue
        style_name = role_styles.get(classification.role)
        if style_name is None:
            continue
        ops: list[Any] = []
        if feature.style_name != style_name:
            ops.append(SetPStyle(style_id="", style_name=style_name))
        if not feature.para.is_empty:
            # tabs survive: signature blocks and leader-dot lines break
            # catastrophically without them.
            keep = ("sectPr", "pageBreakBefore")
            if feature.para.has_tabs and "\t" in feature.text:
                keep += ("tabs",)
            ops.append(StripPPr(keep=keep))
        ops.append(StripRPr(keep_span_emphasis=True))
        if not ops:
            continue
        plan.edits.append(Edit(
            path=feature.path, role=classification.role,
            confidence=classification.confidence,
            locator=locators.of(feature.block), ops=tuple(ops),
            rule_ids=(f"/styles/paragraph/{style_name}",),
            evidence=classification.evidence,
        ))
