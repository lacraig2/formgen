"""RFC 6901 JSON Pointers, used as the key space for everything learned.

Every observation, vote, evidence entry and override pin is addressed by a
pointer into profile.json. That gives one vocabulary across the whole tool:
`overrides.yaml` pins a pointer, `evidence.json` is keyed by pointer, and a
lint finding cites the pointer of the rule that fired.

Escaping is not decoration. Style names are user data and routinely contain
slashes ("Heading 1/2" is a real style in engineering templates); an unescaped
slash would silently split one style into two levels of nesting.
"""

from __future__ import annotations

from typing import Any, Iterable


def escape(token: str) -> str:
    """RFC 6901: ~ becomes ~0 and / becomes ~1, in that order."""
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    """Reverse of `escape`. ~1 first, so ~01 round-trips to ~1."""
    return token.replace("~1", "/").replace("~0", "~")


def ptr(*tokens: Any) -> str:
    """Build a pointer from raw tokens, escaping each."""
    return "".join("/" + escape(str(t)) for t in tokens)


def split(pointer: str) -> list[str]:
    """Pointer -> unescaped tokens. An empty pointer is the whole document."""
    if not pointer:
        return []
    if not pointer.startswith("/"):
        raise ValueError(f"not a JSON Pointer: {pointer!r}")
    return [unescape(t) for t in pointer[1:].split("/")]


def leaf(pointer: str) -> str:
    """The last token, unescaped -- what quantization granularity keys on."""
    tokens = split(pointer)
    return tokens[-1] if tokens else ""


def resolve(document: Any, pointer: str, default: Any = None) -> Any:
    """Read a pointer out of nested dicts/lists, or `default` if absent."""
    node = document
    for token in split(pointer):
        if isinstance(node, dict):
            if token not in node:
                return default
            node = node[token]
        elif isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return default
        else:
            return default
    return node


def assign(document: dict, pointer: str, value: Any) -> None:
    """Write a value at a pointer, creating intermediate dicts as needed."""
    tokens = split(pointer)
    if not tokens:
        raise ValueError("cannot assign to the root pointer")
    node = document
    for token in tokens[:-1]:
        nxt = node.get(token)
        if not isinstance(nxt, dict):
            nxt = {}
            node[token] = nxt
        node = nxt
    node[tokens[-1]] = value


def nest(flat: dict[str, Any]) -> dict:
    """Turn a flat pointer->value map into the nested document it addresses."""
    out: dict = {}
    for pointer, value in sorted(flat.items()):
        assign(out, pointer, value)
    return out


def flatten(document: Any, prefix: str = "") -> dict[str, Any]:
    """Inverse of `nest`: every leaf of a document, keyed by pointer."""
    if isinstance(document, dict) and document:
        out: dict[str, Any] = {}
        for key, value in document.items():
            out.update(flatten(value, prefix + "/" + escape(str(key))))
        return out
    return {prefix: document}


def under(pointers: Iterable[str], prefix: str) -> list[str]:
    """Pointers at or below `prefix`, in sorted order.

    Matching is on whole tokens: "/styles/heading 1" must not match
    "/styles/heading 10", which a plain startswith would.
    """
    prefix = prefix.rstrip("/")
    return sorted(
        p for p in pointers if p == prefix or p.startswith(prefix + "/")
    )
