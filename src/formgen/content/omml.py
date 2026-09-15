"""A TeX subset -> Office Math (OMML).

Full LaTeX is not the goal and pretending otherwise would be dishonest: this
covers what engineering prose actually contains -- fractions, roots, sub- and
superscripts, Greek letters, the common operators and delimiters -- and
reports anything it does not understand rather than silently mangling it.

The reporting matters more than the coverage. An unrecognised construct comes
through as literal text *and* raises a warning, so the author sees "\\oint was
emitted as text" in the output of `new` and can decide. Equations that quietly
render as something else are the worst possible failure here, because nobody
proofreads an equation they did not expect to be wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import qn

# TeX control sequences that are just a character in disguise.
SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ",
    "eta": "η", "theta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "ϕ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ",
    "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "times": "×", "cdot": "·", "div": "÷", "pm": "±",
    "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈",
    "equiv": "≡", "propto": "∝", "sim": "∼",
    "infty": "∞", "partial": "∂", "nabla": "∇",
    "in": "∈", "notin": "∉", "subset": "⊂",
    "rightarrow": "→", "to": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "leftrightarrow": "↔",
    "ldots": "…", "dots": "…", "cdots": "⋯",
    "degree": "°", "circ": "∘", "prime": "′",
    "angle": "∠", "perp": "⊥", "parallel": "∥",
    "quad": " ", "qquad": "  ", ",": " ", ";": " ",
    "!": "", " ": " ",
}

# Large operators that take limits.
BIG_OPERATORS = {
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬",
    "oint": "∮", "bigcup": "⋃", "bigcap": "⋂",
    "lim": "lim", "max": "max", "min": "min",
}

# Function names that set upright.
FUNCTIONS = {
    "sin", "cos", "tan", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
    "log", "ln", "exp", "det", "dim", "gcd", "deg", "arg", "sup", "inf",
}

_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|[{}^_]|\s+|[^\\{}^_\s]")


@dataclass
class Conversion:
    element: etree._Element
    warnings: list[str] = field(default_factory=list)


def tokenize(tex: str) -> list[str]:
    return [t for t in _TOKEN.findall(tex) if not t.isspace() or t == " "]


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0
        self.warnings: list[str] = []

    def peek(self) -> str | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self) -> str | None:
        token = self.peek()
        if token is not None:
            self.pos += 1
        return token

    # -- parsing ------------------------------------------------------

    def parse(self, stop: str | None = None) -> list[etree._Element]:
        out: list[etree._Element] = []
        while (token := self.peek()) is not None:
            if stop is not None and token == stop:
                return out
            if token == "}":
                return out
            node = self.atom()
            if node is None:
                continue
            node = self.scripts(node)
            out.append(node)
        return out

    def group(self) -> list[etree._Element]:
        """The next argument: a braced group, or one atom."""
        if self.peek() == "{":
            self.take()
            out = self.parse()
            if self.peek() == "}":
                self.take()
            return out
        node = self.atom()
        return [node] if node is not None else []

    def atom(self) -> etree._Element | None:
        token = self.take()
        if token is None:
            return None
        if token == "{":
            children = self.parse()
            if self.peek() == "}":
                self.take()
            return _wrap(children)
        if token.startswith("\\"):
            return self.control(token[1:])
        if token in ("^", "_"):
            # A script with nothing before it: emit it literally rather than
            # dropping the operand the author wrote after it.
            self.warnings.append(f"{token!r} with nothing to attach it to")
            return _run(token)
        return _run(token)

    def control(self, name: str) -> etree._Element | None:
        if name == "frac" or name == "dfrac" or name == "tfrac":
            return _fraction(_wrap(self.group()), _wrap(self.group()))
        if name == "sqrt":
            degree = None
            if self.peek() == "[":
                self.take()
                inner: list[str] = []
                while (token := self.peek()) is not None and token != "]":
                    inner.append(self.take() or "")
                self.take()
                degree = _run("".join(inner))
            return _radical(_wrap(self.group()), degree)
        if name in ("text", "mathrm", "mathbf", "mathit", "operatorname", "mbox"):
            return _run(_literal(self.group()), upright=True)
        if name in ("left", "right"):
            delimiter = self.take() or ""
            if name == "left":
                return self.delimited(delimiter)
            return None
        if name in BIG_OPERATORS:
            return _run(BIG_OPERATORS[name], upright=name in ("lim", "max", "min"))
        if name in FUNCTIONS:
            return _run(name, upright=True)
        if name in SYMBOLS:
            return _run(SYMBOLS[name])
        if len(name) == 1 and not name.isalnum():
            return _run(name)          # an escaped literal such as \% or \$
        self.warnings.append(
            f"\\{name} is not in the supported TeX subset and was emitted as text"
        )
        return _run("\\" + name)

    def delimited(self, opening: str) -> etree._Element:
        children: list[etree._Element] = []
        closing = ")"
        while (token := self.peek()) is not None:
            if token == "\\right":
                self.take()
                closing = self.take() or ")"
                break
            node = self.atom()
            if node is None:
                continue
            children.append(self.scripts(node))
        return _delimiter(_wrap(children), opening, closing)

    def scripts(self, base: etree._Element) -> etree._Element:
        sub = sup = None
        while self.peek() in ("^", "_"):
            marker = self.take()
            argument = _wrap(self.group())
            if marker == "^":
                sup = argument
            else:
                sub = argument
        if sub is not None and sup is not None:
            return _sub_sup(base, sub, sup)
        if sup is not None:
            return _script(base, sup, "sSup", "sup")
        if sub is not None:
            return _script(base, sub, "sSub", "sub")
        return base


def _literal(nodes: list[etree._Element]) -> str:
    return "".join("".join(node.itertext()) for node in nodes)


# -- OMML construction ----------------------------------------------------


def _run(text: str, upright: bool = False) -> etree._Element:
    run = etree.Element(qn("m:r"))
    if upright:
        # Without this Word italicises it, and "sin" in italics reads as the
        # product of three variables.
        properties = etree.SubElement(run, qn("m:rPr"))
        style = etree.SubElement(properties, qn("m:sty"))
        style.set(qn("m:val"), "p")
    node = etree.SubElement(run, qn("m:t"))
    node.set(qn("xml:space"), "preserve")
    node.text = text
    return run


def _wrap(children: list[etree._Element]) -> etree._Element:
    if len(children) == 1:
        return children[0]
    holder = etree.Element(qn("m:e"))
    for child in children:
        holder.append(child)
    return holder


def _as_e(node: etree._Element) -> etree._Element:
    if node.tag == qn("m:e"):
        return node
    holder = etree.Element(qn("m:e"))
    holder.append(node)
    return holder


def _fraction(numerator, denominator) -> etree._Element:
    fraction = etree.Element(qn("m:f"))
    etree.SubElement(fraction, qn("m:fPr"))
    num = etree.SubElement(fraction, qn("m:num"))
    num.append(numerator)
    den = etree.SubElement(fraction, qn("m:den"))
    den.append(denominator)
    return fraction


def _radical(radicand, degree=None) -> etree._Element:
    radical = etree.Element(qn("m:rad"))
    properties = etree.SubElement(radical, qn("m:radPr"))
    hide = etree.SubElement(properties, qn("m:degHide"))
    hide.set(qn("m:val"), "0" if degree is not None else "1")
    deg = etree.SubElement(radical, qn("m:deg"))
    if degree is not None:
        deg.append(degree)
    radical.append(_as_e(radicand))
    return radical


def _script(base, argument, tag: str, part: str) -> etree._Element:
    node = etree.Element(qn(f"m:{tag}"))
    etree.SubElement(node, qn(f"m:{tag}Pr"))
    node.append(_as_e(base))
    holder = etree.SubElement(node, qn(f"m:{part}"))
    holder.append(argument)
    return node


def _sub_sup(base, sub, sup) -> etree._Element:
    node = etree.Element(qn("m:sSubSup"))
    etree.SubElement(node, qn("m:sSubSupPr"))
    node.append(_as_e(base))
    lower = etree.SubElement(node, qn("m:sub"))
    lower.append(sub)
    upper = etree.SubElement(node, qn("m:sup"))
    upper.append(sup)
    return node


def _delimiter(content, opening: str, closing: str) -> etree._Element:
    node = etree.Element(qn("m:d"))
    properties = etree.SubElement(node, qn("m:dPr"))
    begin = etree.SubElement(properties, qn("m:begChr"))
    begin.set(qn("m:val"), opening)
    end = etree.SubElement(properties, qn("m:endChr"))
    end.set(qn("m:val"), closing)
    node.append(_as_e(content))
    return node


# -- the entry points -----------------------------------------------------


def to_omml(tex: str, display: bool = False) -> Conversion:
    """Convert TeX to an `m:oMath` (inline) or `m:oMathPara` (display)."""
    parser = _Parser(tokenize(tex))
    children = parser.parse()
    math = etree.Element(qn("m:oMath"))
    for child in children:
        math.append(child)
    if not display:
        return Conversion(math, parser.warnings)
    paragraph = etree.Element(qn("m:oMathPara"))
    paragraph.append(math)
    return Conversion(paragraph, parser.warnings)


def to_tex(math: etree._Element) -> str:
    """Best-effort OMML -> TeX, for `extract`.

    Deliberately lossy and deliberately simple: an equation that came from
    Word rather than from us has structure we never wrote, so round-tripping
    it perfectly is not achievable. Recovering the symbols and the obvious
    structure keeps the Markdown readable and editable, which is the point.
    """
    # A control sequence needs one trailing space to separate it from what
    # follows; the source usually has one too, and two reads as a typo.
    return re.sub(r"(\\[A-Za-z]+) +", r"\1 ", _to_tex(math)).strip()


def _build_reverse() -> dict[str, str]:
    """Unicode -> TeX name, for extract.

    Identity and spacing entries are excluded: SYMBOLS maps "\\," to a thin
    space, and reversing that would turn every ordinary space in an equation
    into a control sequence.
    """
    out: dict[str, str] = {}
    for name, value in list(SYMBOLS.items()) + list(BIG_OPERATORS.items()):
        if not value or value.isspace() or value == name or not name.isalpha():
            continue
        out.setdefault(value, name)
    return out


_REVERSE = _build_reverse()


def _to_tex(node: etree._Element) -> str:
    tag = etree.QName(node).localname
    if tag == "r":
        text = "".join(
            child.text or "" for child in node.findall(qn("m:t"))
        )
        # An upright run holding a known function name came from \sin, not
        # from three variables multiplied together.
        upright = node.find(f"{qn('m:rPr')}/{qn('m:sty')}")
        if (upright is not None and upright.get(qn("m:val")) == "p"
                and text in FUNCTIONS):
            return f"\\{text} "
        return _reverse_symbols(text)
    if tag == "t":
        return _reverse_symbols(node.text or "")
    if tag == "f":
        num = _child_text(node, "num")
        den = _child_text(node, "den")
        return f"\\frac{{{num}}}{{{den}}}"
    if tag == "rad":
        return f"\\sqrt{{{_child_text(node, 'e')}}}"
    if tag == "sSup":
        return f"{_child_text(node, 'e')}^{{{_child_text(node, 'sup')}}}"
    if tag == "sSub":
        return f"{_child_text(node, 'e')}_{{{_child_text(node, 'sub')}}}"
    if tag == "sSubSup":
        return (f"{_child_text(node, 'e')}_{{{_child_text(node, 'sub')}}}"
                f"^{{{_child_text(node, 'sup')}}}")
    if tag == "d":
        return f"\\left({_child_text(node, 'e')}\\right)"
    return "".join(_to_tex(child) for child in node
                   if isinstance(child.tag, str))


def _reverse_symbols(text: str) -> str:
    return "".join(f"\\{_REVERSE[c]} " if c in _REVERSE else c for c in text)


def _child_text(node: etree._Element, name: str) -> str:
    child = node.find(qn(f"m:{name}"))
    return _to_tex(child).strip() if child is not None else ""
