"""The command line.

click rather than argparse for one reason above the others: `CliRunner` gives
every command a fast in-process test, exit codes and stderr included, so the
CLI surface is covered by the same suite as the library rather than by hand.

Two conventions run through every command. Output is ASCII-safe by default
(see report.console -- a cp1252 console raises on the curly quotes that come
out of real documents), and every failure exits with a typed code from
errors.py rather than a traceback.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .errors import (
    FormgenError, InputError, InvariantError, UsageError,
)
from .learn.pipeline import learn as run_learn
from .opc.errors import PackageError
from .opc.package import OpcPackage
from .profile import io as pio
from .profile.sync import sync as run_sync
from .report.console import Console

VERSION = "0.1.0"

DOCX = click.Path(exists=True, dir_okay=False, path_type=Path)
DIRECTORY = click.Path(file_okay=False, path_type=Path)


def _console(ctx: click.Context) -> Console:
    return Console(unicode=ctx.obj.get("unicode", False))


def _fail(console: Console, exc: FormgenError) -> None:
    """Report a typed error and exit with its code -- never a traceback."""
    console.write(f"error: {exc.message}")
    if exc.remedy:
        console.write(f"  {exc.remedy}")
    raise SystemExit(exc.exit_code)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--unicode", is_flag=True,
              help="Emit real Unicode instead of ASCII-folded output.")
@click.version_option(VERSION, prog_name="formgen")
@click.pass_context
def cli(ctx: click.Context, unicode: bool) -> None:
    """Learn a house .docx format, then check and generate against it."""
    ctx.ensure_object(dict)
    ctx.obj["unicode"] = unicode


# -- learn ----------------------------------------------------------------


@cli.command()
@click.argument("exemplars", nargs=-1, type=DOCX)
@click.option("--out", "-o", type=DIRECTORY, required=True,
              help="Profile directory to create or refresh.")
@click.option("--name", default=None, help="Profile name (default: directory name).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable summary.")
@click.pass_context
def learn(ctx, exemplars, out, name, as_json):
    """Infer a format profile from example documents.

    Writes a donor template.docx plus a JSON sidecar. Touches none of the
    inputs.
    """
    console = _console(ctx)
    try:
        result = run_learn([Path(p) for p in exemplars], Path(out), name)
    except FormgenError as exc:
        _fail(console, exc)
        return

    if as_json:
        console.write(json.dumps({
            "profile": str(result.directory),
            "documents": list(result.consensus.docs),
            "donor": result.donor.doc if result.donor else None,
            "properties": len(result.consensus),
            "enforced": len(result.consensus.enforceable()),
            "needs_review": result.needs_review,
            "warnings": list(result.consensus.warnings),
        }, indent=2, sort_keys=True))
        return

    consensus = result.consensus
    console.write(
        f"Learned {len(consensus)} properties from "
        f"{len(consensus.docs)} document(s) -> {result.directory}"
    )
    by_severity: dict[str, int] = {}
    for vote in consensus.votes.values():
        by_severity[vote.severity] = by_severity.get(vote.severity, 0) + 1
    console.write(
        "  lint: "
        + ", ".join(
            f"{by_severity.get(s, 0)} {s}"
            for s in ("error", "error-if-present", "warn", "info", "off")
        )
    )
    console.write(f"  {result.needs_review} need review")
    if result.donor:
        console.write(f"  donor: {result.donor.explain()}")
    if result.scrub_report:
        console.write(f"  scrub: {result.scrub_report.summary()}")
    console.blank()

    for warning in consensus.warnings:
        console.bullet(warning)
    for note in result.notes:
        console.bullet(note)
    if consensus.warnings or result.notes:
        console.blank()

    review = consensus.needs_review()
    if review:
        console.write(f"NEEDS REVIEW ({len(review)})")
        for pointer, vote in sorted(review.items())[:10]:
            console.write(f"  {vote.explain()}")
        if len(review) > 10:
            console.write(f"  ... {len(review) - 10} more; see README.md")
        console.blank()
    console.write("Open template.docx in Word to correct it, then run:")
    console.write(f"  formgen profile sync {result.directory}")
    if result.needs_review:
        raise SystemExit(1)


# -- profile --------------------------------------------------------------


@cli.group()
def profile() -> None:
    """Inspect and correct a learned profile."""


@profile.command("sync")
@click.argument("directory", type=click.Path(exists=True, file_okay=False,
                                             path_type=Path))
@click.pass_context
def profile_sync(ctx, directory):
    """Re-read template.docx and pin whatever a human changed in Word."""
    console = _console(ctx)
    try:
        report = run_sync(Path(directory))
    except FileNotFoundError as exc:
        _fail(console, UsageError(str(exc)))
        return
    except FormgenError as exc:
        _fail(console, exc)
        return
    console.blank()
    console.write(report.render())


@profile.command("show")
@click.argument("directory", type=click.Path(exists=True, file_okay=False,
                                             path_type=Path))
@click.option("--all", "show_all", is_flag=True, help="Every rule, not the top 30.")
@click.pass_context
def profile_show(ctx, directory, show_all):
    """Print what a profile enforces."""
    console = _console(ctx)
    document = pio.read_profile(Path(directory))
    rules = document.get("rules") or {}
    console.write(f"{document.get('name')}  ({len(rules)} properties)")
    console.write(
        f"  learned from {document['corpus']['count']} document(s): "
        + ", ".join(document["corpus"]["documents"])
    )
    console.blank()
    rows = [
        (p, str(body["value"]), body["status"], body["severity"],
         f"{body['agreement']:.0%}", f"{body['observed_in']}/{body['of']}")
        for p, body in sorted(rules.items())
        if show_all or body["severity"] != "off"
    ]
    console.table(
        rows if show_all else rows[:30],
        ("property", "value", "status", "lint", "agree", "docs"),
    )
    if not show_all and len(rows) > 30:
        console.blank()
        console.write(f"  ... {len(rows) - 30} more; --all to see them")


# -- lint -----------------------------------------------------------------


@cli.command()
@click.argument("documents", nargs=-1, type=DOCX, required=True)
@click.option("--profile", "-p", "profile_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Profile directory produced by `formgen learn`.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable report.")
@click.option("--verbose", "-v", is_flag=True,
              help="Show the evidence behind each classification.")
@click.option("--max-severity", type=click.Choice(["error", "warn", "info"]),
              default="error", show_default=True,
              help="Lowest severity that makes the command fail.")
@click.pass_context
def lint(ctx, documents, profile_dir, as_json, verbose, max_severity):
    """Check documents against a profile. Changes nothing."""
    from .plan.builder import build_plan
    from .plan.model import SEVERITY_ORDER
    from .report.console import render_plan
    from .report.jsonout import plan_dict

    console = _console(ctx)
    profile = pio.Profile.load(Path(profile_dir))
    threshold = SEVERITY_ORDER[max_severity]
    plans = []
    worst = 0
    for path in documents:
        try:
            pkg = OpcPackage.open(Path(path))
        except PackageError as exc:
            _fail(console, InputError(f"{Path(path).name}: {exc}"))
            return
        plan = build_plan(pkg, profile, document_name=Path(path).name)
        plans.append(plan)
        if plan.refusals:
            worst = max(worst, 3)
        elif any(f.rank <= threshold for f in plan.findings):
            worst = max(worst, 1)
        if not as_json:
            if len(documents) > 1:
                console.blank()
                console.rule()
            render_plan(plan, console, verbose=verbose)

    if as_json:
        payload = [plan_dict(p) for p in plans]
        console.write(json.dumps(payload if len(payload) > 1 else payload[0],
                                 indent=2, sort_keys=True, ensure_ascii=False))
    if worst:
        raise SystemExit(worst)


# -- apply ----------------------------------------------------------------


@cli.command()
@click.argument("documents", nargs=-1, type=DOCX, required=True)
@click.option("--profile", "-p", "profile_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out-dir", type=DIRECTORY, default=None,
              help="Write outputs here instead of beside the inputs.")
@click.option("--in-place", is_flag=True,
              help="Overwrite the input. Implies --backup; refuses without a TTY.")
@click.option("--backup/--no-backup", default=None,
              help="Keep a timestamped copy plus an undo manifest.")
@click.option("--force", is_flag=True, help="Allow --in-place without a terminal.")
@click.option("--dry-run", is_flag=True,
              help="Show exactly what would change, and write nothing.")
@click.option("--mark-uncertain/--no-mark-uncertain", default=None,
              help="Comment on paragraphs we were unsure about "
                   "(default: on when run interactively).")
@click.option("--on-low-confidence", type=click.Choice(["keep", "restyle"]),
              default="keep", show_default=True,
              help="What to do with a classification below 0.55 confidence.")
@click.pass_context
def apply(ctx, documents, profile_dir, out_dir, in_place, backup, force,
          dry_run, mark_uncertain, on_low_confidence):
    """Re-format documents to match a profile."""
    import sys as _sys

    from .plan.builder import build_plan
    from .plan.execute import execute
    from .report.console import render_plan
    from .safety import backup as backup_mod
    from .safety import guards
    from .safety.verify import snapshot, verify

    console = _console(ctx)
    profile = pio.Profile.load(Path(profile_dir))
    if not profile.template.exists():
        _fail(console, UsageError(f"no {pio.TEMPLATE} in {profile_dir}"))
        return
    if in_place and backup is None:
        backup = True
    if mark_uncertain is None:
        mark_uncertain = _sys.stdin.isatty() and not dry_run
    if out_dir:
        Path(out_dir).mkdir(parents=True, exist_ok=True)

    worst = 0
    for source in documents:
        source = Path(source)
        try:
            guards.check_source(source)
            destination = guards.output_path(source, out_dir, in_place)
            notes = [] if dry_run else guards.check_writable(
                destination, in_place=in_place, force=force
            )
            pkg = OpcPackage.open(source)
            plan = build_plan(pkg, profile, document_name=source.name)
            if plan.refusals:
                render_plan(plan, console)
                worst = max(worst, 3)
                continue
            if dry_run:
                console.blank()
                render_plan(plan, console)
                console.blank()
                console.write(f"  --dry-run: nothing written. "
                              f"{len(plan.edits)} block(s) would change.")
                continue

            before = snapshot(pkg)
            donor = OpcPackage.open(profile.template)
            report = execute(pkg, donor, plan,
                             on_low_confidence=on_low_confidence,
                             mark_uncertain=mark_uncertain)
            faults = verify(
                before, pkg,
                allowed_gains={"comment anchors": report.comments_added},
            )
            if faults:
                _fail(console, InvariantError(
                    f"{source.name}: the reformatted document failed its "
                    f"structural checks, so nothing was written:\n    "
                    + "\n    ".join(faults[:6])
                ))
                return

            saved = backup_mod.make_backup(destination) \
                if backup and destination.exists() else None
            pkg.save(destination, deterministic=True)
            if backup:
                backup_mod.write_manifest(
                    source, destination, saved, profile=profile.name,
                    profile_sha256=profile.template_sha or "", version=VERSION,
                )
            _render_apply(console, source, destination, plan, report, notes)
            if plan.needs_review:
                worst = max(worst, 1)
        except FormgenError as exc:
            _fail(console, exc)
            return
        except PackageError as exc:
            _fail(console, InputError(f"{source.name}: {exc}"))
            return
    if worst:
        raise SystemExit(worst)


def _render_apply(console, source, destination, plan, report, notes):
    console.blank()
    console.write(f"{source.name} -> {destination}")
    console.write(f"  {report.summary()}")
    if report.graft.summary() != "nothing to graft":
        console.write(f"  {report.graft.summary()}")
    if report.lists_added or report.lists_reused:
        console.write(f"  lists: {report.lists_reused} matched the profile, "
                      f"{report.lists_added} carried over")
    if report.comments_added:
        console.write(f"  {report.comments_added} uncertain paragraph(s) "
                      "commented in the output -- open in Word, Next Comment.")
    for note in notes + report.graft.notes:
        console.bullet(note)
    if report.unmatched_styles:
        console.bullet(
            f"{len(report.unmatched_styles)} style(s) had no counterpart in the "
            f"profile: {', '.join(report.unmatched_styles[:5])}"
        )
    review = plan.needs_review
    if review:
        console.blank()
        console.write(f"NEEDS REVIEW ({len(review)})")
        for i, edit in enumerate(review[:5], 1):
            console.write(f"  {i}. {edit.locator.describe()}")
            console.write(f'     Find: "{edit.locator.find_string}"')
            console.write(f"     read as {edit.role} "
                          f"(confidence {edit.confidence:.2f}); left unchanged")
        if len(review) > 5:
            console.write(f"  ... {len(review) - 5} more")


@cli.command()
@click.argument("document", type=DOCX)
@click.pass_context
def undo(ctx, document):
    """Restore the original of a document formgen rewrote."""
    from .safety.backup import undo as run_undo

    console = _console(ctx)
    try:
        restored = run_undo(Path(document))
    except FormgenError as exc:
        _fail(console, exc)
        return
    console.write(f"restored {restored}")


# -- doctor ---------------------------------------------------------------


@cli.command()
@click.argument("target", type=click.Path(exists=True, path_type=Path))
@click.option("--pdf", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Also export a PDF for eyeball review.")
@click.option("--timeout", default=240.0, show_default=True,
              help="Seconds to allow Word before killing it.")
@click.pass_context
def doctor(ctx, target, pdf, timeout):
    """Open a document in Word and check our reading of it against Word's.

    Opt-in and skipped with a notice where Word is unavailable; nothing else
    in formgen depends on it.
    """
    from .word.doctor import run as run_doctor

    console = _console(ctx)
    path = Path(target)
    if path.is_dir():
        path = path / pio.TEMPLATE
        if not path.exists():
            _fail(console, UsageError(f"no {pio.TEMPLATE} in {target}"))
            return
    report = run_doctor(path, pdf=pdf, timeout=timeout)
    console.write(report.render())
    if report.skipped:
        return
    if not report.ok:
        raise SystemExit(1)


# -- inspect --------------------------------------------------------------


@cli.command()
@click.argument("document", type=DOCX)
@click.pass_context
def inspect(ctx, document):
    """Show what formgen sees inside a .docx."""
    from .learn.observe import observe

    console = _console(ctx)
    try:
        pkg = OpcPackage.open(Path(document))
    except PackageError as exc:
        _fail(console, InputError(f"{Path(document).name}: {exc}"))
        return
    obs = observe(pkg, Path(document).stem)
    meta = obs.meta

    console.write(f"{Path(document).name}")
    console.write(
        f"  {meta.blocks} blocks, {meta.paragraphs} paragraphs, {meta.runs} runs"
    )
    console.write(
        f"  {meta.styles_defined} styles defined, "
        f"{len(meta.styles_used)} used: {', '.join(meta.styles_used)}"
    )
    console.write(
        f"  {meta.section_count} section(s), {meta.header_parts} header(s), "
        f"{meta.footer_parts} footer(s), {meta.list_count} list definition(s)"
    )
    console.write(f"  direct formatting: {meta.direct_density:.0%} of runs")
    flags = [
        name for name, on in (
            ("text boxes", meta.has_textbox),
            ("content controls", meta.has_content_controls),
            ("comments", meta.has_comments),
            ("tracked changes", meta.has_tracked_changes),
            ("imported content (altChunk)", meta.has_alt_chunk),
        ) if on
    ]
    if flags:
        console.write("  contains: " + ", ".join(flags))
    if meta.disqualified:
        console.blank()
        console.write("REFUSALS")
        for reason in meta.disqualified:
            console.bullet(reason)
        raise SystemExit(3)


def main() -> None:  # pragma: no cover - console entry point
    cli(obj={})
