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
