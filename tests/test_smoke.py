"""Does a real renderer accept what we write?

LibreOffice is not Word, and a successful conversion says nothing about
whether the document *looks* right. What it does say, reliably, is that the
package is not structurally corrupt -- a non-zero exit or an empty PDF
catches the class of mistake that a unit test on our own reader cannot,
because our reader would have to be wrong in the same way twice.

Skipped when `soffice` is absent, which is every developer machine and no CI
machine. `formgen doctor` is the real gate and it needs Word; this is what
Linux can offer in the meantime.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from fixtures import build
from formgen.content.emit import emit
from formgen.content.markdown_in import parse
from formgen.learn.pipeline import learn

SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
needs_soffice = pytest.mark.skipif(
    SOFFICE is None, reason="LibreOffice is not installed")

# It is slow to start and slower under CI contention.
TIMEOUT = int(os.environ.get("FORMGEN_SOFFICE_TIMEOUT", "180"))


def converts(path: Path, tmp_path: Path) -> Path:
    """Convert to PDF, failing loudly on a non-zero exit or an empty result."""
    out = tmp_path / "pdf"
    out.mkdir(exist_ok=True)
    result = subprocess.run(
        [SOFFICE, "--headless", "--norestore",
         f"-env:UserInstallation=file://{tmp_path / 'lo-profile'}",
         "--convert-to", "pdf", "--outdir", str(out), str(path)],
        capture_output=True, text=True, timeout=TIMEOUT,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    pdf = out / (path.stem + ".pdf")
    assert pdf.exists(), f"no PDF produced: {result.stdout}"
    assert pdf.stat().st_size > 1000, "the PDF is empty; the document did not render"
    assert pdf.read_bytes().startswith(b"%PDF-"), "not a PDF"
    return pdf


MARKDOWN = """---
title: Thermal Margin Analysis
report_no: LR-2027-0009
---

<!-- toc -->

# Introduction

The X-7 radiator exceeds its design margin under worst-case loading.[^1]

[^1]: Measured at 340 K over six hours.

The margin was $\\Delta T = 12.4\\,\\mathrm{K}$.

$$\\frac{Q}{A} = \\sigma T^4$$

| Case    | Margin (K) |
|---------|-----------:|
| Nominal |       12.4 |
| Worst   |        3.1 |

## Method

- soak the panel
- measure the margin

1. first
2. second

> A quotation, for the style.

::: distribution-statement
Approved for public release.
:::
"""


def corpus(directory: Path, n: int = 4) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(n):
        body = (
            build.para("Thermal Margin Analysis", style="Title")
            + build.para("Distribution Statement A: approved for public "
                         "release.", style="BodyText")
            + build.para(f"Report No. LR-202{i}-0041", style="BodyText")
            + build.para("Introduction", style="Heading1")
            + build.para("The X-7 radiator exceeds its design margin.",
                         style="BodyText")
        )
        path = directory / f"r{i}.docx"
        build.make(body).save(path, deterministic=True)
        paths.append(path)
    return paths


@needs_soffice
def test_a_fixture_package_renders(tmp_path):
    """Our own writer first: if this fails nothing downstream means anything."""
    path = tmp_path / "fixture.docx"
    build.make().save(path, deterministic=True)
    converts(path, tmp_path)


@needs_soffice
def test_a_learned_template_renders(tmp_path):
    result = learn(corpus(tmp_path / "corpus"), tmp_path / "prof", generated="x")
    converts(result.directory / "template.docx", tmp_path)


@needs_soffice
def test_a_generated_document_renders(tmp_path):
    """The whole emitter at once -- footnotes, fields, math, tables, lists."""
    learn(corpus(tmp_path / "corpus"), tmp_path / "prof", generated="x")
    package, _ = emit(parse(MARKDOWN), tmp_path / "prof" / "template.docx")
    path = tmp_path / "generated.docx"
    package.save(path, deterministic=True)
    converts(path, tmp_path)


@needs_soffice
def test_a_reformatted_document_renders(tmp_path):
    from formgen.opc.package import OpcPackage
    from formgen.plan.builder import build_plan
    from formgen.plan.execute import execute
    from formgen.profile.io import Profile

    learn(corpus(tmp_path / "corpus"), tmp_path / "prof", generated="x")
    profile = Profile.load(tmp_path / "prof")
    foreign = build.make(body=(
        build.para("Thermal Margin Analysis", rpr='<w:sz w:val="56"/><w:b/>')
        + build.para("Introduction", rpr='<w:sz w:val="32"/><w:b/>',
                     ppr_extra="<w:keepNext/>")
        + build.para("Prose that the house format has opinions about.")
    ))
    plan = build_plan(foreign, profile, "foreign.docx")
    execute(foreign, OpcPackage.open(profile.template), plan)
    path = tmp_path / "reformatted.docx"
    foreign.save(path, deterministic=True)
    converts(path, tmp_path)
