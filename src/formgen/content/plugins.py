"""The four markdown-it extensions our dialect needs, written here on purpose.

`mdit-py-plugins` is in the anaconda channel but is not reliably preinstalled
-- it arrives as a dependency of `myst-parser` -- and depending on it would
make "net new installs: none" false. Our dialect needs custom rules regardless
(`::: role`, `{#label}`), so writing the other two costs little and removes the
question entirely.

Four rules:

* **math** -- ``$x$`` inline and ``$$x$$`` as a block. The inline rule refuses
  to open on ``$ `` or close on `` $`` so that "costs $5 and $7" is not read
  as an equation, which is the failure every naive dollar-math implementation
  has.
* **footnotes** -- ``[^1]`` references and ``[^1]: text`` definitions.
* **attributes** -- a trailing ``{#label}`` on a heading, image or table,
  which is what cross-references point at.
* **directives** -- ``::: name`` ... ``:::``, the escape hatch for house
  boilerplate that has no Markdown equivalent.
"""

from __future__ import annotations

import re

_LABEL = re.compile(r"\{#([A-Za-z0-9_:.\-]+)\}\s*$")
_FOOTNOTE_REF = re.compile(r"\[\^([^\]\s]+)\]")
_FOOTNOTE_DEF = re.compile(r"^\[\^([^\]\s]+)\]:\s?(.*)$")
_DIRECTIVE = re.compile(r"^:::+\s*([A-Za-z0-9_-]*)\s*$")


# -- math -----------------------------------------------------------------


def math_plugin(md) -> None:
    md.inline.ruler.before("escape", "math_inline", _math_inline)
    md.block.ruler.before("fence", "math_block", _math_block,
                          {"alt": ["paragraph", "reference", "blockquote"]})
    md.add_render_rule("math_inline", lambda *a, **k: "")
    md.add_render_rule("math_block", lambda *a, **k: "")


def _math_inline(state, silent: bool) -> bool:
    src, start = state.src, state.pos
    if src[start] != "$":
        return False
    if start > 0 and src[start - 1] == "\\":
        return False
    # "$ 5" is money, not maths. Requiring a non-space after the opening
    # delimiter is what keeps prose about dollars out of the equation editor.
    if start + 1 >= len(src) or src[start + 1].isspace():
        return False

    pos = start + 1
    while pos < len(src):
        if src[pos] == "$" and src[pos - 1] != "\\" and not src[pos - 1].isspace():
            break
        if src[pos] == "\n":
            return False
        pos += 1
    else:
        return False
    if pos == start + 1:
        return False

    if not silent:
        token = state.push("math_inline", "math", 0)
        token.content = src[start + 1:pos]
        token.markup = "$"
    state.pos = pos + 1
    return True


def _math_block(state, start_line: int, end_line: int, silent: bool) -> bool:
    start = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    if not state.src[start:maximum].startswith("$$"):
        return False
    first = state.src[start + 2:maximum].strip()
    line = start_line
    content: list[str] = []

    if first.endswith("$$") and len(first) > 2:
        content = [first[:-2].strip()]
    else:
        if first:
            content.append(first)
        line += 1
        while line < end_line:
            text = state.src[
                state.bMarks[line] + state.tShift[line]:state.eMarks[line]
            ]
            if text.strip().endswith("$$"):
                stripped = text.strip()[:-2].strip()
                if stripped:
                    content.append(stripped)
                break
            content.append(text)
            line += 1
        else:
            return False

    if silent:
        return True
    token = state.push("math_block", "math", 0)
    token.block = True
    token.content = "\n".join(content).strip()
    token.markup = "$$"
    token.map = [start_line, line + 1]
    state.line = line + 1
    return True


# -- footnotes ------------------------------------------------------------


def footnote_plugin(md) -> None:
    md.inline.ruler.after("image", "footnote_ref", _footnote_ref)
    md.block.ruler.before("reference", "footnote_def", _footnote_def,
                          {"alt": ["paragraph", "reference"]})
    md.add_render_rule("footnote_ref", lambda *a, **k: "")


def _footnote_ref(state, silent: bool) -> bool:
    match = _FOOTNOTE_REF.match(state.src, state.pos)
    if not match:
        return False
    if not silent:
        token = state.push("footnote_ref", "", 0)
        token.meta = {"label": match.group(1)}
    state.pos = match.end()
    return True


def _footnote_def(state, start_line: int, end_line: int, silent: bool) -> bool:
    start = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    match = _FOOTNOTE_DEF.match(state.src[start:maximum])
    if not match:
        return False
    if silent:
        return True

    label, first = match.group(1), match.group(2)
    lines = [first] if first else []
    line = start_line + 1
    # A definition continues while the following lines are indented, which is
    # how a footnote holds more than one paragraph.
    while line < end_line:
        if state.isEmpty(line):
            if line + 1 < end_line and state.tShift[line + 1] >= 4:
                lines.append("")
                line += 1
                continue
            break
        if state.tShift[line] < 4 and lines:
            break
        text = state.src[
            state.bMarks[line] + state.tShift[line]:state.eMarks[line]
        ]
        lines.append(text)
        line += 1

    token = state.push("footnote_def", "", 0)
    token.meta = {"label": label, "content": "\n".join(lines).strip()}
    token.map = [start_line, line]
    token.block = True
    state.line = line
    return True


# -- {#label} attributes --------------------------------------------------


def attributes_plugin(md) -> None:
    md.core.ruler.after("inline", "formgen_labels", _labels)


def _labels(state) -> None:
    """Strip a trailing {#label} and hang it on the enclosing block token."""
    tokens = state.tokens
    for index, token in enumerate(tokens):
        if token.type != "inline" or not token.children:
            continue
        label = _take_label(token)
        if not label:
            continue
        owner = tokens[index - 1] if index else token
        owner.meta = dict(owner.meta or {})
        owner.meta["label"] = label
        token.meta = dict(token.meta or {})
        token.meta["label"] = label


def _take_label(token) -> str | None:
    for child in reversed(token.children):
        if child.type != "text":
            # An image carries its own label, which the image rule reads from
            # the text that follows it.
            if child.type == "image":
                continue
            return None
        match = _LABEL.search(child.content)
        if not match:
            if child.content.strip():
                return None
            continue
        child.content = child.content[:match.start()].rstrip()
        return match.group(1)
    return None


# -- ::: directives -------------------------------------------------------


def directive_plugin(md) -> None:
    md.block.ruler.before("fence", "directive", _directive,
                          {"alt": ["paragraph", "reference", "blockquote", "list"]})


def _directive(state, start_line: int, end_line: int, silent: bool) -> bool:
    start = state.bMarks[start_line] + state.tShift[start_line]
    maximum = state.eMarks[start_line]
    match = _DIRECTIVE.match(state.src[start:maximum].rstrip())
    if not match or not match.group(1):
        return False
    if silent:
        return True

    name = match.group(1)
    line = start_line + 1
    depth = 1
    body: list[str] = []
    while line < end_line:
        text = state.src[state.bMarks[line] + state.tShift[line]:state.eMarks[line]]
        closing = _DIRECTIVE.match(text.rstrip())
        if closing is not None:
            if closing.group(1):
                depth += 1
            else:
                depth -= 1
                if depth == 0:
                    break
        body.append(text)
        line += 1

    open_token = state.push("directive_open", "div", 1)
    open_token.meta = {"name": name}
    open_token.map = [start_line, line]
    open_token.block = True
    state.md.block.parse("\n".join(body), state.md, state.env, state.tokens)
    close_token = state.push("directive_close", "div", -1)
    close_token.block = True
    state.line = line + 1
    return True


def formgen_plugins(md):
    """Everything our dialect adds to CommonMark, in one call."""
    math_plugin(md)
    footnote_plugin(md)
    attributes_plugin(md)
    directive_plugin(md)
    return md
