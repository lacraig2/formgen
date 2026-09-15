"""The plan: pure data describing what is wrong and what would fix it.

One engine sits behind `lint`, `apply` and `apply --dry-run`:

    load -> refuse-checks -> walk -> featurize -> classify -> PLAN -> [render | execute]

`lint` renders the plan and exits. `apply` executes it. `apply --dry-run`
renders it through lint's own renderer. That makes "the linter reports exactly
what the reformatter would change" a structural guarantee rather than a
discipline two people have to maintain in two files.

Nothing here mutates anything. An `Op` is a description of an edit, not the
edit; `plan.execute` (Phase 3) is the only code allowed to touch a tree. The
separation is what makes a dry run trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .locator import Locator

# Severity ordering, worst first. `lint` exits non-zero on ERROR.
ERROR, WARN, INFO = "error", "warn", "info"
SEVERITY_ORDER = {ERROR: 0, WARN: 1, INFO: 2}


@dataclass(frozen=True)
class Finding:
    """One deviation from the profile."""

    code: str                  # dotted rule family, e.g. "style.size"
    severity: str
    message: str
    locator: Locator
    rule_id: str = ""          # the profile pointer that produced it
    expected: Any = None
    actual: Any = None
    fixable: bool = False
    confidence: float = 1.0
    evidence: tuple[str, ...] = ()

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 3)

    def render(self) -> list[str]:
        lines = [f"  [{self.severity}] {self.code}: {self.message}"]
        lines.extend(self.locator.render())
        if self.confidence < 0.55:
            lines.append(
                f"     (low confidence {self.confidence:.2f} -- "
                "this classification is a guess)"
            )
        return lines


# -- operations (described in Phase 2, executed in Phase 3) --------------


@dataclass(frozen=True)
class Op:
    """Base class. Subclasses are data; execution lives in plan.execute."""

    def describe(self) -> str:  # pragma: no cover - overridden
        return type(self).__name__


@dataclass(frozen=True)
class SetPStyle(Op):
    style_id: str
    style_name: str = ""

    def describe(self) -> str:
        return f"apply style {self.style_name or self.style_id}"


@dataclass(frozen=True)
class StripRPr(Op):
    """Remove presentational run properties.

    `keep_span_emphasis` implements the span-scope rule: a property covering
    the whole paragraph is presentational and goes; one covering a proper
    sub-span is emphasis and stays. That single distinction correctly keeps
    "the *p*-value was significant" and correctly discards a whole-paragraph
    bold heading.
    """

    properties: tuple[str, ...] = ()
    keep_span_emphasis: bool = True

    def describe(self) -> str:
        return f"strip direct run formatting ({', '.join(self.properties) or 'all'})"


@dataclass(frozen=True)
class StripPPr(Op):
    keep: tuple[str, ...] = ()

    def describe(self) -> str:
        kept = f", keeping {', '.join(self.keep)}" if self.keep else ""
        return f"replace paragraph properties with the style's{kept}"


@dataclass(frozen=True)
class RemapNumId(Op):
    from_num_id: int
    to_num_id: int
    ilvl: int = 0

    def describe(self) -> str:
        return f"move list {self.from_num_id} to the house list at level {self.ilvl}"


@dataclass(frozen=True)
class RemapRStyle(Op):
    """Character styles are remapped, never stripped: that is how Hyperlink,
    FootnoteReference and CommentReference survive a reformat."""

    from_style_id: str
    to_style_id: str

    def describe(self) -> str:
        return f"remap character style {self.from_style_id} -> {self.to_style_id}"


@dataclass(frozen=True)
class GraftPart(Op):
    """Carry a format part from the donor into the document's package."""

    part: str
    reltype: str

    def describe(self) -> str:
        return f"graft {self.part} from the profile"


@dataclass(frozen=True)
class Edit:
    """Everything that would happen to one block."""

    path: str
    role: str
    confidence: float
    locator: Locator
    ops: tuple[Op, ...] = ()
    rule_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    @property
    def needs_review(self) -> bool:
        return self.confidence < 0.55

    def render(self) -> list[str]:
        head = f"  {self.role} ({self.confidence:.2f})"
        return [head] + [f"     - {op.describe()}" for op in self.ops] + \
               self.locator.render()


@dataclass
class Plan:
    """The whole answer for one document."""

    document: str = ""
    profile: str = ""
    findings: list[Finding] = field(default_factory=list)
    edits: list[Edit] = field(default_factory=list)
    graft_ops: list[Op] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    refusals: list[str] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    def extend(self, findings: Iterable[Finding]) -> None:
        self.findings.extend(findings)

    def sorted_findings(self) -> list[Finding]:
        """Worst first, then in document order -- the order people read in."""
        return sorted(
            self.findings,
            key=lambda f: (f.rank, f.locator.part, f.locator.path, f.code),
        )

    def counts(self) -> dict[str, int]:
        out = {ERROR: 0, WARN: 0, INFO: 0}
        for finding in self.findings:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out

    @property
    def errors(self) -> int:
        return self.counts()[ERROR]

    @property
    def needs_review(self) -> list[Edit]:
        return [e for e in self.edits if e.needs_review]

    @property
    def exit_code(self) -> int:
        if self.refusals:
            return 3
        return 1 if self.errors else 0
