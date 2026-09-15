"""`formgen profile sync` -- read the donor back after a human edited it.

This is the correction path, and it is a first-class part of the design rather
than an escape hatch: `learn` infers a convention from examples, so it *will*
be wrong somewhere, and the fix has to be usable by someone who knows Word and
has no intention of reading JSON.

So the user opens `template.docx`, changes the thing that is wrong using the
Styles pane or the Layout tab, and saves. `sync` re-reads the donor, diffs it
against what the corpus voted for, and records every intentional deviation as
a **pin** in `overrides.yaml` -- which the next `learn` re-applies on top of a
freshly derived profile. That layering is the whole point: corrections must
survive re-learning with a bigger corpus, or nobody will make them twice.

Placeholders get the same treatment. `learn` materialises each one as a
content control tagged `formgen.<name>`, so adding, removing or renaming a
placeholder is a Developer-tab operation in Word, and `sync` reads the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..analyze.stats import bucket
from ..learn.observe import observe
from ..opc.ns import qn
from ..opc.package import OpcPackage
from ..util.pointer import leaf
from . import io as pio
from .schema import decode, encode, is_derived

PLACEHOLDER_PREFIX = "formgen."


@dataclass
class Divergence:
    """One property where the donor and the profile disagree."""

    pointer: str
    donor: Any
    profile: Any

    def describe(self) -> str:
        return (
            f"{self.pointer}  {encode(self.profile, self.pointer)} -> "
            f"{encode(self.donor, self.pointer)}"
        )


@dataclass
class SyncReport:
    changed: bool = False
    old_sha: str | None = None
    new_sha: str | None = None
    divergences: list[Divergence] = field(default_factory=list)
    placeholders_added: tuple[str, ...] = ()
    placeholders_removed: tuple[str, ...] = ()
    styles_read: int = 0
    sections_read: int = 0
    controls_read: int = 0
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.changed and not self.divergences:
            return "template.docx is unchanged; nothing to sync."
        lines = []
        if self.old_sha and self.new_sha:
            lines.append(
                f"  template.docx changed  ({self.old_sha[:4]}..{self.old_sha[-2:]} "
                f"-> {self.new_sha[:4]}..{self.new_sha[-2:]})"
            )
        lines.append(
            f"  re-read {self.styles_read} styles, {self.sections_read} sections, "
            f"{self.controls_read} content controls"
        )
        lines.append("")
        if self.divergences:
            lines.append(
                f"  {len(self.divergences)} deviation(s) from learned consensus "
                "-> pinned in overrides.yaml"
            )
            for d in self.divergences:
                lines.append(f"    {d.describe()}")
        else:
            lines.append("  no deviations from the learned consensus")
        for name in self.placeholders_added:
            lines.append(f"    placeholders.{name}  added (content control)")
        for name in self.placeholders_removed:
            lines.append(f"  1 placeholder removed:  {name}")
        lines += self.notes
        lines.append("")
        lines.append("  Run `formgen lint` to see what this changes.")
        return "\n".join(lines)


def donor_values(pkg: OpcPackage) -> dict[str, Any]:
    """What the donor itself says, in the same key space as the profile."""
    return observe(pkg, "template").single()


def placeholders_in(pkg: OpcPackage) -> dict[str, dict]:
    """Content controls tagged for us, keyed by placeholder name.

    Only `formgen.`-prefixed tags count. A document's own unrelated content
    controls -- Word's cover-page date picker, a corporate add-in's field --
    must not be mistaken for placeholders we are meant to fill.
    """
    out: dict[str, dict] = {}
    root = pkg.element(pkg.main_document)
    for sdt in root.iter(qn("w:sdt")):
        pr = sdt.find(qn("w:sdtPr"))
        if pr is None:
            continue
        tag_el = pr.find(qn("w:tag"))
        tag = tag_el.get(qn("w:val")) if tag_el is not None else None
        if not tag or not tag.startswith(PLACEHOLDER_PREFIX):
            continue
        name = tag[len(PLACEHOLDER_PREFIX):]
        alias_el = pr.find(qn("w:alias"))
        entry: dict[str, Any] = {"tag": tag}
        if alias_el is not None and alias_el.get(qn("w:val")):
            entry["label"] = alias_el.get(qn("w:val"))
        for kind, key in (("w:date", "date"), ("w:comboBox", "choice"),
                          ("w:dropDownList", "choice"), ("w:picture", "image"),
                          ("w:richText", "rich_text"), ("w:text", "text")):
            if pr.find(qn(kind)) is not None:
                entry["type"] = key
                break
        entry.setdefault("type", "text")
        if pr.find(qn("w:lock")) is not None:
            # A locked control bounces the user's own edits, which defeats the
            # entire correction workflow. Flag it rather than fixing silently.
            entry["locked"] = True
        out[name] = entry
    return out


def sync(directory: Path, today: str | None = None) -> SyncReport:
    """Re-read template.docx, pin its deviations, and report every one."""
    directory = Path(directory)
    template = directory / pio.TEMPLATE
    report = SyncReport()
    if not template.exists():
        raise FileNotFoundError(f"no {pio.TEMPLATE} in {directory}")

    profile = pio.read_profile(directory)
    report.old_sha = (profile.get("template") or {}).get("sha256")
    report.new_sha = pio.sha256_of(template)
    report.changed = report.old_sha != report.new_sha

    pkg = OpcPackage.open(template)
    observed = donor_values(pkg)
    rules: dict[str, dict] = profile.get("rules") or {}
    report.styles_read = len({
        p.split("/")[3] for p in observed if p.startswith("/styles/")
        and len(p.split("/")) > 3
    })
    report.sections_read = int(observed.get("/page/section_count") or 1)

    overrides = pio.Overrides.load(directory / pio.OVERRIDES)
    for pointer in sorted(rules):
        if pointer not in observed or is_derived(pointer):
            # Derived values follow from the ones they are computed from, so
            # pinning one would leave a stale contradiction behind the moment
            # its inputs change.
            continue
        learned = decode(rules[pointer].get("value"), pointer)
        current = observed[pointer]
        name = leaf(pointer)
        if bucket(current, name) == bucket(learned, name):
            continue
        report.divergences.append(Divergence(pointer, current, learned))
        overrides.add_pin(
            pointer, current,
            note="read back from template.docx", source="sync", today=today,
        )

    controls = placeholders_in(pkg)
    report.controls_read = len(controls)
    known = set(overrides.placeholders)
    report.placeholders_added = tuple(sorted(set(controls) - known))
    report.placeholders_removed = tuple(sorted(known - set(controls)))
    for name, entry in controls.items():
        merged = dict(overrides.placeholders.get(name) or {})
        merged.update(entry)
        overrides.placeholders[name] = merged
        if entry.get("locked"):
            report.notes.append(
                f"    placeholders.{name} is a LOCKED content control; "
                "unlock it in Word (Developer > Properties) or edits will bounce."
            )
    for name in report.placeholders_removed:
        overrides.placeholders.pop(name, None)

    report.notes += overrides.dump(directory / pio.OVERRIDES)
    return report
