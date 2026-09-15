"""Build a corpus for the Word QA job, from the same fixtures the tests use.

Real documents are better and belong in `tests/corpus/`; this exists so the
scheduled Word job has something to chew on when there are none to check in,
and because generating the corpus with our own writer means the writer is
exercised on every run rather than only in unit tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tests"))
# Works from a checkout as well as from an install, so the QA job does not
# depend on which of the two it got.
sys.path.insert(0, str(_ROOT / "src"))

from fixtures import build            # noqa: E402


# Names that vary in shape, not just in a trailing digit: a corpus whose
# authors are "Author 0" and "Author 1" splits into the literal "Author " and
# a one-character field, which tests nothing.
AUTHORS = ["L. Craig", "R. Patel", "K. Ito", "M. Rao", "S. Bell", "J. Okonkwo"]


def exemplar(index: int):
    body = (
        build.para("Thermal Margin Analysis", style="Title")
        + build.para("Distribution Statement A: approved for public release.",
                     style="BodyText")
        + build.para(f"Report No. LR-202{index}-0041", style="BodyText")
        + build.para(f"Prepared by {AUTHORS[index % len(AUTHORS)]}",
                     style="BodyText")
        + build.para("Introduction", style="Heading1")
        + build.para("The X-7 radiator exceeds its design margin under "
                     "worst-case loading.", style="BodyText")
        + build.para("Methods", style="Heading1")
        + build.para("The panel was soaked at 340 K for six hours before "
                     "measurement.", style="BodyText")
    )
    return build.make(body, creator=AUTHORS[index % len(AUTHORS)])


def main(directory: str, count: int = 6) -> int:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        exemplar(index).save(out / f"report{index}.docx", deterministic=True)
    print(f"wrote {count} exemplars to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
