"""Self-refilling documents: a filled `.docx` that carries its own blank form.

A normal fill is one-way -- once ``{{name}}`` becomes "K. Ito" the marker is
gone, so you cannot reopen the finished document and change an answer. This
tucks two things into the finished file, in a part Word ignores and formgen
preserves byte-for-byte:

* the **original blank template** (markers intact), so the form can be filled
  afresh rather than reverse-engineered from filled text;
* the **answers** last used, so reopening the document shows the form already
  populated.

Reopen such a file and formgen rebuilds the form from the embedded template and
pre-fills it; change an answer and the newly filled document carries an updated
copy of the same pair. The visible document is always a clean fill -- the
template and answers ride alongside it, not inside its body.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ..opc.errors import PackageError
from ..opc.package import OpcPackage

# Its own top-level part, unreferenced by the document body: Word neither reads
# nor needs it, and formgen copies untouched parts verbatim, so it survives a
# round-trip through this tool untouched.
SESSION_PART = "/formgen/session.json"
CONTENT_TYPE = "application/json"
VERSION = 1


def read_session(pkg: OpcPackage) -> dict | None:
    """The embedded ``{template, answers}`` if this document carries one."""
    if SESSION_PART not in pkg:
        return None
    try:
        data = json.loads(pkg.blob(SESSION_PART))
    except (PackageError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("template"), str):
        return None
    return data


def write_session(pkg: OpcPackage, template: bytes, answers: dict[str, Any]) -> None:
    """Embed (or replace) the blank template and the answers in `pkg`."""
    payload = json.dumps({
        "v": VERSION,
        "template": base64.b64encode(template).decode("ascii"),
        "answers": answers,
    }).encode("utf-8")
    if SESSION_PART in pkg:
        pkg.replace_part(SESSION_PART, payload)
    else:
        pkg.add_part(SESSION_PART, payload, CONTENT_TYPE)


def template_of(session: dict) -> bytes:
    """The blank template bytes carried by a session."""
    return base64.b64decode(session["template"])


def encode_answers(text: dict | None, checks: dict | None,
                   images: dict | None, groups: dict | None) -> dict:
    """Fold the four value channels into one JSON-safe answer set.

    Image bytes become base64: a top-level image is ``{name: b64}``, a
    repeating-row image cell is ``{"image": b64}`` -- the same shape the form
    already sends, so reloading is symmetric with filling."""
    return {
        "text": dict(text or {}),
        "checks": {k: bool(v) for k, v in (checks or {}).items()},
        "images": {k: base64.b64encode(v).decode("ascii")
                   for k, v in (images or {}).items()
                   if isinstance(v, (bytes, bytearray))},
        "groups": _encode_groups(groups),
    }


def _encode_groups(groups: dict | None) -> dict:
    encoded: dict = {}
    for collection, records in (groups or {}).items():
        if not isinstance(records, list):
            continue
        rows = []
        for record in records:
            if not isinstance(record, dict):
                continue
            row = {}
            for key, value in record.items():
                if isinstance(value, (bytes, bytearray)):
                    row[key] = {"image": base64.b64encode(value).decode("ascii")}
                else:
                    row[key] = value
            rows.append(row)
        encoded[collection] = rows
    return encoded
