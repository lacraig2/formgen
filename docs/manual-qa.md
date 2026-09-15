# Manual QA

Run this on a Windows box with Word before every release. It exists because
**rendering fidelity is the one thing CI cannot assert**: LibreOffice will
convert a document that Word offers to repair, and a document can be
structurally perfect and still look wrong.

Budget about 45 minutes. Record the result in the release notes, including
what you skipped and why — a partial pass recorded honestly is worth more
than a full pass nobody actually ran.

Environment:

- Windows, unprivileged, **Anaconda 2023.07-2** (Python 3.11.4)
- Word installed, with an interactive desktop session
- `pip install --no-deps -e .` — if anything else needs installing, that is a
  finding, not a setup step

---

## 0. Before anything else

```bat
python -c "import importlib.util as u; [print(f'{m:20} {\"yes\" if u.find_spec(m) else \"NO\"}') for m in ['lxml','markdown_it','yaml','click','dateutil','PIL','pytest','win32com']]"
python -m pytest -q
```

- [ ] Every module reads `yes`. A `NO` means the "net new installs: none"
      claim has broken and the release is blocked until it is true again.
- [ ] The suite passes on the target machine, not only on CI.

## 1. `learn` — on real documents, not fixtures

Use 6–12 real reports from one house format. Do not clean them up first; the
mess is the point.

```bat
formgen learn C:\reports\*.docx -o profiles\lab-report
```

- [ ] It finishes in under a minute or two, and says how many properties it
      learned, which document it chose as donor, and why.
- [ ] `README.md` reads like something you could hand to a colleague.
- [ ] Every warning is true. Check two or three against the documents.
- [ ] If it reported a corpus split ("your 12 exemplars split into 2
      formats"), open one document from each group and confirm they really
      are different formats.
- [ ] If required placeholder names came out below confidence, the exit code
      is 1 and the artifacts are **still written**.

### The console is the classic crash

Run the same command in `cmd.exe` with `chcp 1252`, and again with a document
containing curly quotes, em dashes and a non-Latin name.

- [ ] No `UnicodeEncodeError`. Anywhere. This is the #1 Windows failure.
- [ ] `--unicode` renders the nicer characters when you ask for them.

## 2. `doctor` — let Word grade our homework

```bat
formgen doctor profiles\lab-report\template.docx --pdf template.pdf
```

- [ ] Word opens `template.docx` **with no repair prompt**. A repair prompt
      is a release blocker, full stop.
- [ ] The style comparison reports no divergence between Word's resolution
      and ours. A divergence in `basedOn`, `docDefaults` or theme fonts is a
      bug in `oox/styles.py`, not a doctor bug.
- [ ] The exported PDF looks like the house format. Actually look at it.
- [ ] No `WINWORD.EXE` is left running afterwards (check Task Manager). An
      orphan holds file locks and silently breaks the next run.

## 3. Correcting the profile — the loop that makes this usable

Open `profiles\lab-report\template.docx` in Word.

- [ ] Change the body font size via Styles → Modify Style. Save. Close.
- [ ] `formgen profile sync profiles\lab-report` reports exactly that change
      and pins it in `overrides.yaml`.
- [ ] Developer tab → the inferred placeholders are there as real content
      controls, correctly named, and **clickable and editable** (a locked one
      is a bug).
- [ ] Rename one control's tag. Run `sync`. It is picked up.
- [ ] Re-run `learn` with two more exemplars. Your pin survives, the renamed
      placeholder keeps its new name, and any pin the bigger corpus now
      contradicts is **reported**, not silently kept or silently dropped.

## 4. `lint` — on a document you already know is wrong

Pick a document a colleague has complained about.

- [ ] Every finding is true. Read them all; there should be few enough to.
- [ ] Paste a `find_string` into Word's Find box. It finds exactly one
      place, and it is the right place.
- [ ] The heading path in the report matches what Word's Navigation pane
      shows — including for a document whose headings are not styled.
- [ ] Nothing is reported as an error that the corpus was actually split on.
- [ ] `formgen explain doc.docx --at "some text"` explains a finding you did
      not expect, well enough that you agree with it or find the bug.

## 5. `apply` — the one that rewrites someone's document

Use a document with comments, footnotes, a table, images, a hyperlink and an
equation. A converted-from-Google-Docs or converted-from-PDF file is the
hardest case and the most realistic one.

```bat
formgen apply foreign.docx -p profiles\lab-report
```

- [ ] The output is `foreign.formatted.docx`. The input is **byte-identical**
      to what it was.
- [ ] Word opens the output with no repair prompt.
- [ ] Open both side by side. Every comment, footnote, image, hyperlink,
      equation and bookmark is still there and still anchored to the same
      text.
- [ ] Emphasis inside sentences survived; whole-paragraph bold did not.
- [ ] Signature blocks and leader-dot lines did not collapse (tabs).
- [ ] Numbered lists restart where they should and did not merge with an
      unrelated list.
- [ ] `--mark-uncertain` put real Word comments on the uncertain paragraphs,
      and Next Comment walks them.
- [ ] Run it a second time on the output: nothing changes.
- [ ] `formgen lint` on the output reports no errors.

### And the refusals

- [ ] A document with tracked changes is **refused**, with a remedy.
- [ ] A protected document is refused, with a remedy.
- [ ] `--in-place` on a document open in Word refuses and says to close it —
      a message, not a traceback.
- [ ] `formgen undo` restores it. Edit the output first, then try again: it
      refuses rather than discarding your edit.

## 6. `new` and `extract`

```bat
formgen new report.md -p profiles\lab-report -o report.docx
```

- [ ] The cover page is filled from the front matter.
- [ ] A field you did **not** supply is empty or shows `[[MISSING: name]]` —
      never the donor's own value from whatever report it came from.
- [ ] The TOC is an unpopulated field with a visible "press F9" line. Press
      F9 in Word: it builds correctly and the page numbers are real.
- [ ] Figure and table numbers renumber correctly after F9.
- [ ] Cross-references jump to the right place (Ctrl+click).
- [ ] Footnotes are numbered, at the bottom of the right page.
- [ ] Equations render as real Word equations (click one — it should be
      editable in the equation editor, not a picture).
- [ ] `formgen lint report.docx -p profiles\lab-report` reports nothing.
- [ ] `formgen extract report.docx` gives back Markdown close to the input.

## 7. Paths and the filesystem

- [ ] A file in a deep SharePoint tree (>260 characters) works.
- [ ] A OneDrive file that has not been downloaded is refused with a useful
      message rather than stalling.
- [ ] A path with a space and a non-ASCII character in it works.
- [ ] `--out-dir` to a network share works.

---

## Recording the result

```
Release:     0.1.0
Machine:     <host>, Word <version>, Anaconda 2023.07-2
Date:        <date>
Sections:    1-7 run; 7 partially (no SharePoint access)
Blockers:    none
Findings:    #41 lint reports the cover table's label cells as body text
```
