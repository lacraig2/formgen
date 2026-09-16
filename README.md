# formgen

Learn a `.docx` house format from examples, then generate, check, and repair
against it.

House formats live in people's heads and in "copy last quarter's report and
overwrite the text". That is lossy: direct formatting accumulates, headings
drift, margins wander, and nobody can say authoritatively what the format
*is*. `formgen` turns it into an artifact you can inspect, diff, correct and
enforce.

```
formgen learn   reports/*.docx -o profiles/lab-report   # infer the format
formgen lint    draft.docx -p profiles/lab-report       # check, change nothing
formgen apply   foreign.docx -p profiles/lab-report     # re-format
formgen new     report.md -p profiles/lab-report        # Markdown -> .docx
formgen extract report.docx                             # .docx -> Markdown
```

Plus `explain` (why was this paragraph classified that way?), `inspect`,
`undo`, `profile sync`, and `doctor`.

## The profile is a real `.docx`

```
profiles/lab-report/
  template.docx    # the donor -- THE FORMAT ITSELF. Edit it in Word.
  overrides.yaml   # your corrections. Never regenerated.
  profile.json     # generated. Never hand-edit.
  evidence.json    # generated -- the distributions behind every decision
  corpus.json      # generated -- exemplar hashes, agreement, warnings
  README.md        # generated -- the format, in prose
```

`template.docx` is made by copying the best exemplar's zip byte for byte and
deleting things, never by synthesizing OOXML from JSON. Latent styles,
`w:tblStylePr` conditional table formatting, `w:link`ed character styles,
`w:numStyleLink`, compat settings, embedded fonts and theme tint/shade maths
all ride along for free, byte-identical — and the single largest corruption
surface in docx tooling, cross-package part copying, is removed from the
codebase rather than handled.

## Word is the editor

`learn` will get things wrong. That is inherent to inferring a convention
from examples, so the correction path is a first-class part of the design:

| What is wrong | Where you fix it | Word tool |
|---|---|---|
| Fonts, sizes, spacing, indents | `template.docx` | Styles pane |
| Headings, lists, numbering | `template.docx` | Styles, Multilevel List |
| Margins, columns, headers, footers | `template.docx` | Layout, Header & Footer |
| **Which fields are placeholders** | `template.docx` | Developer → content controls |
| Cover page and boilerplate | `template.docx` | just type in it |
| Lint severity, tolerances, muted rules | `overrides.yaml` | text editor |
| Placeholder type, regex, required-ness | `overrides.yaml` | text editor |

`formgen profile sync` reads the donor back and records every intentional
deviation as a pin. **Pins survive re-learning**: run `learn` again with
twenty more exemplars and your corrections are re-applied on top, with any
pin the bigger corpus now contradicts *reported* rather than silently kept or
silently dropped. A tool that loses your fix the second time you use it is a
tool you stop trusting.

## What it will not do

- **It will not need your documents to have styles.** 48% of real `.docx`
  use two or fewer paragraph styles, because Google Docs export, PDF
  conversion and hand-formatting all leave everything as `Normal`. Paragraphs
  vote by classified role as well as by style name, so the format is
  recovered either way -- and the roles it learns are written into the donor
  as real styles, because a format that cannot be pointed at cannot be
  applied.
- **It will not guess quietly.** Every learned property carries two numbers —
  coverage (how many documents had an opinion) and agreement (how many agreed)
  — and they are never collapsed into one. A style in 3 of 12 documents is
  rare, not contested, and the difference decides whether lint fires.
- **It will not fabricate a page number.** TOC, `SEQ` and `REF` go in as
  dirty fields with a visible "press F9". With Word present, `--refresh`
  populates them properly.
- **It will not rewrite your input.** Output is `<stem>.formatted.docx`;
  `--in-place` implies `--backup`, refuses when Word has the file open, and
  refuses without a terminal unless you force it.
- **It will not mangle rather than refuse.** Tracked changes, document
  protection and `w:altChunk` are refused, each naming a remedy.
- **It will not require Word.** The core is pure OOXML. Word is an opt-in
  layer that improves output and verification and is never on the critical
  path; dev and CI are Linux.

## Re-formatting is a package graft

The profile holds format parts (`styles.xml`, `numbering.xml`, `theme1.xml`,
`sectPr`, headers and footers); your document holds content parts. `apply`
carries the *format into the content package*, so every `r:id`, footnote id,
comment anchor and bookmark pair in the body stays valid because it was never
moved.

> The graft's worst case is **residue** — a stray `w:shd` survives, visible,
> lintable, fixable next run. The rebuild's worst case is **deletion** —
> invisible and unrecoverable. For a tool that rewrites other people's
> documents, choose the strategy whose worst case is "not clean enough".

Runs are never rebuilt; only `w:pPr` and per-run `w:rPr` are edited in place.
That single invariant is what keeps fields, bookmarks, comment ranges, OMML
and `w:ins` intact. A run property covering the whole paragraph is
presentational and is dropped; one covering a proper sub-span is emphasis and
is kept — which correctly keeps "the *p*-value was significant" and correctly
discards **A WHOLE BOLD HEADING**.

## It runs in a browser, unmodified

The template-to-document half -- filling a form, rendering Markdown into the
house format -- runs under [Pyodide](https://pyodide.org) with no port, no
bindings and no shared subset: the same `.py` files, loaded into
WebAssembly.

```
cd tools/wasm && npm install && node check.mjs
  CPython : 10 paragraphs, 1 table, 1 footnote, 1 equation
  WASM    : 10 paragraphs, 1 table, 1 footnote, 1 equation   (67 source files, unmodified)
    identical  filled.docx     1ecb09322d0210d7...
    identical  generated.docx  cb96bb040120b65e...
```

Byte-identical output is the whole claim, and CI enforces it. The alternative
is a second implementation in JavaScript, which would have to re-derive the
OPC preservation guarantee, the content-control handling, the field mechanics
and the deterministic zip -- and would then drift from the Python one in ways
nobody notices until a document is wrong.

Everything except `word/` works this way. The Word COM layer is the one part
that cannot: it is lazily imported and never on the critical path, so its
absence costs a browser nothing the way it costs Linux nothing.

## Installing

Python 3.10+. On the target — Windows, unprivileged, Anaconda 2023.07-2 —
**everything it needs is already installed**:

```
lxml  markdown-it-py  PyYAML  click  python-dateutil  Pillow
pywin32 (optional, Windows only: doctor, --refresh, --pdf)
```

```bat
pip install --no-deps -e .
```

CI enforces the no-new-installs claim rather than asserting it.

## Tests

```
pytest -q                    # 631 tests
pytest -q tests/test_smoke.py -rs   # + LibreOffice, if it is installed
```

The invariant suite is production code: the same checks run in tests and as
`apply`'s pre-write verification stage. Text preserved, counts preserved,
referential integrity, `apply(apply(x)) == apply(x)`, `lint(apply(x))` clean,
`lint(new(md))` clean, `extract(emit(md)) ≈ md`.

`docs/manual-qa.md` is the per-release checklist for a Windows box with Word,
because rendering fidelity is the one thing CI cannot assert.
`docs/open-questions.md` records the judgement calls and why they went the
way they did.
