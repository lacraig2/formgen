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
import yaml

from .errors import (
    FormgenError, InputError, InvariantError, RefusalError, UsageError,
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
            "skeleton": (result.skeleton.as_json()
                         if result.skeleton is not None else None),
            "unconfident": [s.name for s in result.unconfident],
        }, indent=2, sort_keys=True))
        if result.unconfident:
            raise SystemExit(1)
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
    if result.skeleton is not None:
        skeleton = result.skeleton
        console.write(
            f"  structure: {len(skeleton.required_sections)} required "
            f"section(s), {len(skeleton.boilerplate)} fixed passage(s), "
            f"{len(skeleton.placeholders)} placeholder(s)"
        )
        if result.materialized and result.materialized.wrapped:
            console.write(
                f"  {len(result.materialized.wrapped)} placeholder(s) written "
                "into template.docx as content controls"
            )
    console.blank()

    if result.skeleton is not None and result.skeleton.placeholders:
        console.write("PLACEHOLDERS")
        for slot in result.skeleton.placeholders:
            console.write(f"  {slot.describe()}")
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
    unconfident = result.unconfident
    if unconfident:
        # The artifacts are written either way -- refusing to write them
        # would leave nothing to correct. The exit code is what forces a
        # human through this review exactly once instead of never.
        console.write(f"CONFIRM THESE NAMES ({len(unconfident)})")
        for slot in unconfident:
            console.write(f"  {slot.name}: guessed from {slot.name_source}")
            if slot.examples:
                console.write(f"    e.g. {', '.join(slot.examples[:3])}")
        console.write(
            "  Open template.docx in Word, Developer tab, and check each "
            "control's tag; then run `formgen profile sync`."
        )
        console.blank()

    console.write("Open template.docx in Word to correct it, then run:")
    console.write(f"  formgen profile sync {result.directory}")
    if result.needs_review or unconfident:
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


# -- new ------------------------------------------------------------------


def _parse_set(pairs: tuple[str, ...]) -> dict:
    out: dict = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise UsageError(f"--set expects key=value, got {pair!r}")
        out[key.strip()] = value
    return out


@cli.command()
@click.argument("source", type=click.Path(exists=True, dir_okay=False,
                                          path_type=Path))
@click.option("--profile", "-p", "profile_dir", required=True,
              type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out", "-o", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Output .docx (default: alongside the source).")
@click.option("--set", "overrides", multiple=True, metavar="KEY=VALUE",
              help="Supply or override a front-matter field.")
@click.option("--allow-missing", is_flag=True,
              help="Emit a visible [[MISSING: key]] instead of refusing.")
@click.option("--cover/--no-cover", default=True,
              help="Keep the template's cover page (default: keep).")
@click.pass_context
def new(ctx, source, profile_dir, out, overrides, allow_missing, cover):
    """Render a Markdown document into the house format."""
    from .content.emit import emit as run_emit
    from .content.markdown_in import parse, validate
    from .safety import guards
    from .safety.verify import check_integrity

    console = _console(ctx)
    profile = pio.Profile.load(Path(profile_dir))
    if not profile.template.exists():
        _fail(console, UsageError(f"no {pio.TEMPLATE} in {profile_dir}"))
        return
    destination = Path(out) if out else Path(source).with_suffix(".docx")

    try:
        text = Path(source).read_text(encoding="utf-8-sig")
        doc = parse(text)
        doc.meta.update(_parse_set(overrides))
        required = profile.required_placeholders()
        problems = validate(doc, required=required, fields=profile.placeholders)
        # A shape mismatch is reported, never blocking: the pattern came from
        # a handful of exemplars, and the first report with a genuinely new
        # numbering scheme has to be publishable.
        advisory = {"footnote.unused", "placeholder.shape",
                    "placeholder.bad_pattern"}
        blocking = [p for p in problems if p.code not in advisory]

        if blocking and not allow_missing:
            console.write(f"{Path(source).name}: cannot render")
            for problem in problems:
                console.bullet(problem.message)
                if problem.remedy:
                    console.write(f"    {problem.remedy}")
            raise SystemExit(2)
        if blocking and allow_missing:
            for key in sorted(required - set(doc.meta)):
                # A visible sentinel, never a silently empty field: an empty
                # one ships, and [[MISSING: reviewer]] does not.
                doc.meta[key] = f"[[MISSING: {key}]]"

        guards.check_writable(destination)
        pkg, report = run_emit(doc, profile.template,
                               source_dir=Path(source).parent, keep_cover=cover)
        faults = check_integrity(pkg)
        if faults:
            _fail(console, InvariantError(
                "the generated document failed its structural checks, so "
                "nothing was written:\n    " + "\n    ".join(faults[:6])
            ))
            return
        pkg.save(destination, deterministic=True)
    except FormgenError as exc:
        _fail(console, exc)
        return
    except ValueError as exc:
        _fail(console, UsageError(f"{Path(source).name}: {exc}"))
        return

    console.write(f"{Path(source).name} -> {destination}")
    console.write(f"  {report.summary()}")
    if report.filled or report.cleared:
        console.write(
            f"  fields: {len(report.filled)} filled"
            + (f", {len(report.cleared)} cleared (the template carried the "
               "donor's own values)" if report.cleared else "")
        )
    for warning in doc.warnings + report.warnings:
        console.bullet(warning)
    for problem in problems:
        if problem.code in advisory:
            console.bullet(problem.message)
            if problem.remedy:
                console.write(f"    {problem.remedy}")
    if report.fields:
        console.write(
            "  fields were inserted unpopulated; press F9 in Word (or re-run "
            "with --refresh once Word support lands) to build them."
        )
    if blocking and allow_missing:
        raise SystemExit(1)


# -- extract --------------------------------------------------------------


@cli.command("extract")
@click.argument("document", type=DOCX)
@click.option("--out", "-o", type=click.Path(dir_okay=False, path_type=Path),
              default=None, help="Output .md (default: alongside the input).")
@click.option("--media-dir", type=DIRECTORY, default=None,
              help="Write embedded images here (default: <stem>.media).")
@click.option("--profile", "-p", "profile_dir", default=None,
              type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="Use a profile's style names to classify paragraphs.")
@click.pass_context
def extract_cmd(ctx, document, out, media_dir, profile_dir):
    """Pull a .docx back to Markdown, so documents can live in git.

    Not a reformatting path: round-tripping through Markdown loses comments,
    tracked changes and embedded objects. Use `apply` to re-format.
    """
    from .content.extract import extract as run_extract
    from .content.extract import to_markdown

    console = _console(ctx)
    path = Path(document)
    destination = Path(out) if out else path.with_suffix(".md")
    media = Path(media_dir) if media_dir else destination.with_suffix("").with_name(
        destination.stem + ".media"
    )
    try:
        pkg = OpcPackage.open(path)
    except PackageError as exc:
        _fail(console, InputError(f"{path.name}: {exc}"))
        return
    styles = None
    if profile_dir:
        styles = pio.Profile.load(Path(profile_dir)).style_names
    doc = run_extract(pkg, media_dir=media, profile_styles=styles)
    destination.write_text(to_markdown(doc), encoding="utf-8")

    console.write(f"{path.name} -> {destination}")
    console.write(
        f"  {len(doc.blocks)} blocks, {len(doc.footnotes)} footnote(s)"
    )
    if media.exists() and any(media.iterdir()):
        console.write(f"  images written to {media}")
    for warning in doc.warnings:
        console.bullet(warning)


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


# -- fill -----------------------------------------------------------------


@cli.command()
@click.argument("document", type=DOCX, required=False)
@click.option("--profile", "-p", "profile_dir", default=None,
              type=click.Path(exists=True, file_okay=False, path_type=str),
              help="Fill this profile's template.docx instead of a document.")
@click.option("--values", "values_file", default=None,
              type=click.Path(exists=True, dir_okay=False, path_type=str),
              help="YAML or JSON file of field values.")
@click.option("--set", "overrides", multiple=True, metavar="KEY=VALUE",
              help="Set one field. Repeatable.")
@click.option("--out", "-o", type=click.Path(dir_okay=False, path_type=str),
              default=None, help="Output .docx (default: <stem>.filled.docx).")
@click.option("--keep-unsupplied", is_flag=True,
              help="Leave fields you said nothing about as they are. "
                   "Refuses on a template, which holds the donor's own values.")
@click.option("--list", "list_only", is_flag=True,
              help="List the fields and change nothing.")
@click.pass_context
def fill(ctx, document, profile_dir, values_file, overrides, out,
         keep_unsupplied, list_only):
    """Fill a form's fields, keeping everything else exactly as it is.

    `new` renders Markdown into a template: it replaces the body, because for
    a report the body is the author's work. A form is the other way round --
    the labels, the table and the layout *are* the document and the values
    are the small part -- so this keeps all of it and changes only the fields.
    """
    from .content.fill import fill as run_fill
    from .learn.formfields import find_fields
    from .safety import guards
    from .safety.verify import check_integrity

    console = _console(ctx)
    if not document and not profile_dir:
        _fail(console, UsageError("give a document or a --profile to fill"))
        return
    source = Path(document) if document else \
        pio.Profile.load(Path(profile_dir)).template
    if not source.exists():
        _fail(console, UsageError(f"{source} does not exist"))
        return

    try:
        pkg = OpcPackage.open(source)
    except PackageError as exc:
        _fail(console, InputError(f"{source.name}: {exc}"))
        return

    if list_only:
        report = find_fields(pkg)
        console.write(f"{source.name}: {len(report.fields)} field(s)")
        for item in report.fields:
            mark = "x" if item.filled else " "
            console.write(f"  [{mark}] {item.describe()}")
        if not report.fields:
            console.write("  none. This document declares no fields; learn a "
                          "profile from several like it and the comparator "
                          "will find what varies.")
        return

    values: dict = {}
    if values_file:
        text = Path(values_file).read_text(encoding="utf-8-sig")
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            _fail(console, UsageError(f"{values_file} is not a mapping"))
            return
        values.update(loaded)
    values.update(_parse_set(overrides))
    if not values:
        _fail(console, UsageError(
            "no values given",
            "pass --values FILE or --set key=value, or use --list to see "
            "what this document's fields are called.",
        ))
        return

    if keep_unsupplied and not document:
        _fail(console, RefusalError(
            "--keep-unsupplied on a profile template would ship the donor's "
            "own values",
            "template.docx is a byte-faithful copy of a real document, so "
            "the fields you do not set still hold that document's answers. "
            "Fill a copy of your own document instead, or set every field.",
        ))
        return

    destination = Path(out) if out else \
        source.with_name(f"{source.stem}.filled{source.suffix}")
    try:
        guards.check_writable(destination)
        report = run_fill(pkg, values, keep_unsupplied=keep_unsupplied)
        faults = check_integrity(pkg)
        if faults:
            _fail(console, InvariantError(
                "the filled document failed its structural checks, so nothing "
                "was written:\n    " + "\n    ".join(faults[:6])))
            return
        pkg.save(destination, deterministic=True)
    except FormgenError as exc:
        _fail(console, exc)
        return

    console.write(f"{source.name} -> {destination}")
    console.write(f"  {report.summary()}")
    if report.unknown:
        console.write(
            f"  {len(report.unknown)} value(s) matched no field: "
            + ", ".join(report.unknown[:6])
        )
        console.write("    run with --list to see what the fields are called.")
    if report.cleared:
        console.write(
            f"  {len(report.cleared)} field(s) cleared -- they held the "
            "original document's values and you gave none."
        )
    if report.unknown:
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


# -- explain --------------------------------------------------------------


@cli.command()
@click.argument("document", type=DOCX)
@click.option("--at", "needle", required=True, metavar="TEXT",
              help="Any distinctive words from the block, as you would type "
                   "them into Word's Find box.")
@click.option("--profile", "-p", "profile_dir", default=None,
              type=click.Path(exists=True, file_okay=False, path_type=str),
              help="Judge the block against this profile as well.")
@click.pass_context
def explain(ctx, document, needle, profile_dir):
    """Show why one block was classified the way it was.

    Prints the block's features, every signal that fired with its weight, the
    winning role, the runners-up, and what `apply` would do to it.
    """
    from .classify.features import build_context
    from .classify.rules import classify_block, signals_for
    from .oox.walk import match_key

    console = _console(ctx)
    try:
        pkg = OpcPackage.open(Path(document))
    except PackageError as exc:
        _fail(console, InputError(f"{Path(document).name}: {exc}"))
        return

    profile = pio.Profile.load(Path(profile_dir)) if profile_dir else None
    ctxd = build_context(pkg)
    wanted = match_key(needle).lower()
    hits = [f for f in ctxd.features
            if f.is_paragraph and wanted in match_key(f.text).lower()]
    if not hits:
        _fail(console, UsageError(
            f"nothing in {Path(document).name} contains {needle!r}",
            "use words you can see in Word's Find box; matching ignores "
            "smart quotes, dashes and repeated spaces.",
        ))
        return
    if len(hits) > 1:
        console.write(f"{len(hits)} blocks match {needle!r}; showing the first.")
        console.write("  Add more words to narrow it down.")
        console.blank()

    features = hits[0]
    known = profile.style_names if profile else None
    placeholders = set(profile.placeholders) if profile else None
    result = classify_block(features, ctxd, known, placeholders)

    console.write(f"{Path(document).name}  {features.path}")
    console.write(f"  text: {features.text[:80]!r}")
    console.write(
        f"  style: {features.style_name or '(none)'}"
        f"  size: {features.run.size or '(inherited)'}"
        f"  font: {features.font or '(inherited)'}"
    )
    console.write(
        f"  {features.words} word(s), "
        f"outline level {features.outline_level if features.outline_level is not None else '-'}, "
        f"numbering {'yes' if features.numbering else 'no'}"
    )
    if ctxd.modal_size:
        console.write(
            f"  this document's body text is {ctxd.modal_size} "
            f"{ctxd.modal_font or ''}".rstrip()
        )
    console.blank()

    console.write("SIGNALS")
    signals = sorted(signals_for(features, ctxd, known, placeholders),
                     key=lambda s: (-s.weight, s.role))
    if not signals:
        console.bullet("none fired; the block falls through to body text.")
    for signal in signals:
        kind = "floor" if signal.prior else "evidence"
        console.write(f"  {signal.weight:.2f} {signal.role:<14} "
                      f"[{kind}] {signal.evidence}")
    console.blank()

    console.write(f"ROLE: {result.role} ({result.confidence:.0%})")
    if result.alternatives:
        console.write("  runners-up: " + ", ".join(
            f"{role} {score:.0%}" for role, score in result.alternatives))
    if result.needs_review:
        console.write("  below the review threshold -- `apply` would leave "
                      "this block alone unless you confirm it.")
    if profile is not None:
        from .plan.builder import _role_to_style

        style = _role_to_style(profile).get(result.role)
        console.blank()
        console.write("AGAINST THE PROFILE")
        console.write(
            f"  {profile.name} puts {result.role} in "
            + (f"the {style!r} style" if style
               else "no style -- it defines none for this role")
        )
        if style and features.style_name != style:
            console.write(f"  this block is {features.style_name or '(none)'}, "
                          f"so `apply` would restyle it.")


def main() -> None:  # pragma: no cover - console entry point
    cli(obj={})
