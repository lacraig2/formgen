# Real documents

Checked-in binaries that are hard in ways a fixture cannot be: converted from
Google Docs or from a PDF, a cover page built out of text boxes, a list whose
numbering was typed by hand, comments attached across a table boundary,
`mc:AlternateContent` around a diagram, an `w:altChunk`.

**They are used for invariant tests only, never golden tests.** A golden test
against a real document would pin our current output as correct, which is the
opposite of what these are for: they exist to assert that text is preserved,
counts are preserved, referential integrity holds, and `apply(apply(x))` is
`apply(x)` — properties that must hold whatever the document is.

The directory is empty in this repository because we have no documents we can
redistribute. When you add one:

- strip anything confidential first, and check the `docProps` as well as the
  body — author names, company, and the revision history all live there;
- add a line here saying what the document is for, in terms of what it
  breaks: "textbox cover page, defeats alignment" is useful, "customer
  report" is not;
- keep it small. It is under version control forever.
