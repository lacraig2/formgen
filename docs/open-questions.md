# Open questions

Behaviour we have implemented one way but cannot verify without Word. Each has
a test pinning the *current* choice, so changing the answer changes a test
rather than silently changing output.

Settle these with `formgen doctor` on the Windows box (Phase 1.5), which drives
Word via COM and can compare Word's own resolution against ours.

## 1. Do `w:ascii` and `w:asciiTheme` inherit independently?

**We assume: no** -- specifying either at a nearer level replaces both.

ECMA 17.3.2.26 reads the other way: *"If this attribute is not present, the
default value is to leave the formatting applied at previous level in the style
hierarchy."* OpenXmlPowerTools contradicts the spec and merges them atomically.

We follow PowerTools because the spec reading leads to an absurdity: if an
`asciiTheme` inherited from `docDefaults` always beat a nearer explicit
`w:ascii`, no run could ever override a themed default font -- yet picking a
non-theme font in Word's font box plainly works.

**Test:** in a document whose `docDefaults` sets `asciiTheme="minorHAnsi"`,
apply Arial to a run from the font box, save, and inspect. If the run renders
Arial, we are right. Pinned by
`test_rfonts_explicit_and_theme_move_together_across_levels`.

## 2. What happens when a theme slot resolves to nothing?

**We assume:** fall back to the explicit typeface.

LibreOffice deliberately does the opposite ("overwrite Fonts_ascii with
Fonts_asciiTheme even if theme font is empty - this is apparently what Word
2013 does"). Neither ECMA-376 nor MS-OI29500 states a rule. Low impact: it only
matters for a malformed or missing theme part.

## 3. `w:themeTint` / `w:themeShade` HSL math

**Not yet implemented.** We currently prefer Word's cached `w:val`, which is
correct in practice and strictly better than ignoring the tint, but wrong if a
theme is swapped after the fact.

When implementing, note two traps: `themeTint` wins when both are present, and
ECMA's published per-channel shade formula is self-inconsistent and diverges
sharply from Word -- use HSL luminance per [MS-OI29500] 2.1.71
(`shade: L' = L*s`, `tint: L' = L*t + (1-t)`, where the hex byte / 255).
Do not share the implementation with DrawingML `a:tint`/`a:shade`, which really
is per-channel linear.

## 4. `firstLine` vs `hanging` when both survive a merge

They are mutually exclusive in Word. We currently let both through. Assumed:
`hanging` wins. Unverified.

## 5. Does `w:numPr` merge element-wise, or replace as a unit?

**We assume: element-wise** -- `w:numId` and `w:ilvl` inherit independently
through the style cascade.

The two readings differ only for a paragraph that specifies one half and
inherits the other:

| style says | paragraph says | element-wise | atomic replacement |
|---|---|---|---|
| numId=3, ilvl=0 | ilvl=2 | (3, 2) | (none, 2) -> unnumbered |
| numId=3, ilvl=2 | numId=3 | (3, 2) | (3, 0) |

ECMA 17.7.2 treats a style's properties as inherited individually, which argues
for element-wise; but `w:numPr` is one property *element*, which argues for
replacement. We chose element-wise on a failure-mode argument rather than a
textual one: getting the level wrong renumbers a paragraph, while dropping the
`numId` removes its numbering entirely, and one of those is recoverable by
eye.

**Test:** in Word, apply a numbered style, demote one paragraph with Tab, save,
and read back the `w:numPr`. Pinned by
`test_direct_ilvl_demotes_within_the_styles_list`.

## 6. Bullet glyph canonicalisation is a lookup table, not a rule

`canonical_bullet` maps Symbol/Wingdings private-use codepoints to the
characters they draw, so that the same bullet in two fonts votes as one value.
The table covers Word's three default bullet levels plus the common dingbats;
anything unlisted passes through unchanged, which is the safe direction (two
identical bullets look different) rather than the unsafe one (two different
bullets merge).

There is no authority to check this against -- it is font metrics, not a
schema. `doctor` can widen it empirically by asking Word to render each level
and reading back what it drew.

## 7. Header inheritance is per (kind, type)

**We assume:** a section that omits a `w:headerReference` of one type inherits
that type alone from the previous section, independently of the other five
slots.

This matches Word's UI, where "Link to Previous" is a separate toggle for the
first-page, even-page and default headers and again for the three footers.
ECMA 17.6.12 does not state the inheritance rule at all -- it is entirely
implementation behaviour. Pinned by
`test_headers_and_footers_inherit_independently`.

## 8. Is a repeated paragraph boilerplate, or a small corpus?

Nothing in the text distinguishes "every exemplar carries this distribution
statement because the format requires it" from "three exemplars happen to
open their Introduction the same way". The consequences are very different:
the first should be enforced word for word, the second must not be, or the
linter tells every author their Introduction is wrong.

We decided on **position**, not text: fixed wording is enforced in the front
matter -- before the first heading -- and never under a heading, and never for
a title. That is where real boilerplate lives (cover, distribution statement,
classification marking), and it is also the region `new` carries verbatim out
of the donor, so generated documents stay clean by construction.

It is a heuristic and it will be wrong for a house format whose boilerplate
sits mid-document -- a standard safety notice before the Methods section, say.
The remedy is the same as everywhere else: the passage still appears in
`profile.json` with its coverage, and `overrides.yaml` can raise its severity.

## 9. Why does a missing required section only warn?

Because nothing in formgen can add one. `apply` reformats what is in front of
it and cannot write a Methods section; `new` renders the Markdown it was
given. An error that no command can clear is one people learn to ignore, and
it would break the `lint(apply(x))` fixed point for no gain. Altered
boilerplate *is* an error, because somebody had the passage and changed it.

## 10. Optional, or forgotten?

Undecidable, and we say so rather than guessing. A column present in 40-75% of
the corpus is marked `needs_review` with the reason spelled out; one below 40%
is not part of the skeleton at all. The only way to settle it is a human
looking at `template.docx`, which is why the profile build fails once until
somebody does.

## 11. Half of real documents have no styles. What then?

Measured, not assumed: of 130 real `.docx` from Apache POI's corpus, **48%
use two or fewer paragraph styles**. Google Docs export, PDF-to-Word
conversion and plain hand-formatting all produce the same thing -- every
paragraph `Normal`, the format written out on the runs.

A style-keyed ballot learns almost nothing from those. The title, the
headings and the body all land in a bucket called `normal`, so the corpus
agrees the body is 11pt and has no opinion at all about what a heading looks
like -- the most visible thing about a house format, invisible.

We added a **second ballot keyed on the classified role**, which is recovered
from appearance and so works precisely where styles do not. On a corpus put
through the converter it recovers the same title/heading/body sizes as the
styled original. Three constraints keep it honest:

- It is observed *alongside* the style-keyed ballot, never instead of it.
  When a document has styles they are the better key, because a style name
  crosses a file boundary and an inferred role is our opinion.
- **A guess is not a ballot.** Only classifications the classifier does not
  flag for review may vote.
- Body-level paragraphs only. A table cell's format is governed by the table
  style, and letting a dense table vote on `body` would drown the prose.

Of body-level paragraphs in the real corpus, the median document has 100% of
them voting and none has under 50%.

## 12. Why write styles into the donor at all?

Because a format that is known but cannot be expressed is half a profile.
`apply` restyles a paragraph by pointing `w:pStyle` at a style; on a
hand-formatted corpus the donor defines none, so there is nowhere to point
and the format cannot be applied even though it was learned.

So roles the corpus agreed on are materialized as styles in the donor. This
also settles a trap that predates the whole question: a `w:pStyle` naming a
style the document does not define falls back to Word's *built-in*
definition of that name, which differs by Word version and by locale. A
profile that says "Heading 1" without defining it is not specifying a format,
it is naming one and hoping.

Two rules: a style the donor already defines is never rebuilt -- the real
article carries conditional formatting, latent-style flags and linked
character styles no reconstruction from JSON could reproduce -- and the
role-to-style mapping is **recorded in profile.json**, not re-derived, so
nothing ever points `w:pStyle` at a style we merely assumed was there.

## 13. Why not a JavaScript implementation for the browser?

The template-to-document half is the part people want client-side: fill a
form, render Markdown into the house format, download a `.docx`, without the
document ever leaving the machine. The obvious way to get that is to write it
in JavaScript.

We ran the Python under Pyodide instead, and the outputs are byte-identical:
the same 67 source files, no port, no bindings, no shared subset.

The reason is not that a JavaScript version would be hard. It is that it
would be a **second implementation of the same guarantees**, and those
guarantees are the whole product. It would have to re-derive the OPC
preservation rule (parts we do not understand are copied byte for byte), the
content-control and legacy-form-field mechanics, the field instruction
handling, the deterministic zip, and the invariant suite -- and the moment
the two drift, the drift shows up as somebody's document being subtly wrong,
which is the failure mode this whole design exists to avoid.

`tools/wasm/check.mjs` compares the two engines byte for byte and CI runs it,
so "it also runs in the browser" is a property that is checked rather than a
claim that was true once.

The cost is real and worth stating: Pyodide is a ~10 MB download before
anything happens, and start-up is seconds rather than milliseconds. For a
page someone opens to fill in one form that is a poor trade; for a tool
somebody keeps open while working through a batch it is a good one. If the
download ever becomes the deciding factor, the answer is to trim the
dependency set -- Pillow is needed only to scale images to the text column --
not to write the whole thing twice.

`word/` is the one part that cannot follow. It is lazily imported and never
on the critical path, so a browser loses exactly what Linux loses: the
optional Word verification layer, and nothing else.

---

## 14. What can the redaction *not* decide?

The donor is somebody's real report. `scrub` takes their name off it;
`learn/redact.py` takes their report out of it, on one rule:

> The donor keeps what the corpus agreed on. What only the donor said is data.

That rule is decidable for body text — a sentence three exemplars share is
boilerplate, a sentence only this one has is the author's. Three things sit
outside it, and each is handled differently on purpose.

**A corpus written by one person.** Every report has their name in the footer,
so every exemplar agrees, so consensus is certain it is house boilerplate. It
is not. No amount of comparing the corpus against itself can tell, because the
corpus is the thing that is biased. The only outside evidence is the identity
we just scrubbed from the document properties, so a kept line containing a
scrubbed name is **reported and left in place** — deleting it would be wrong on
the many formats where the footer really is the house footer.

**Fewer than three exemplars.** There is no agreement to compute, so the body
is kept whole and `learn` says so. Guessing here fails in both directions: too
eager and the template is gutted, too shy and the report ships.

**Names rather than values.** A content control's alias, a bookmark name and a
custom property's name are all kept, because each is something the format
refers to *by name*: a `REF` field points at the bookmark, a `DOCPROPERTY`
field at the property, and deleting either leaves the template with an
unresolvable field. Their *values* go. This is the one place where a
determined leak could survive — a bookmark named after a client — and the
judgment is that breaking every cross-reference in the template is the worse
failure.

Everything unambiguous goes regardless of corpus size: the page-one thumbnail
in `docProps/thumbnail.jpeg`, the custom XML store a content control was bound
to, cached field results, custom property values, `TitlesOfParts`, footnotes
whose anchor was removed, and any media, embedding or external link left with
nothing pointing at it.

The second-order effect matters as much as the first. `apply` grafts the
donor's headers and footers into *other people's* documents. Before this pass,
a house format learned from twelve lab reports would have stamped the donor's
own report number into the header of every document it touched.
