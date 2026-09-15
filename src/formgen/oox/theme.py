"""theme1.xml -- resolving font and colour indirection.

Styles rarely name a typeface directly; they reference a theme slot
(w:asciiTheme="minorHAnsi") which resolves through the theme's font scheme.
Colours do the same via w:themeColor. Every value we store in a profile is the
*resolved* concrete value, with the theme token kept alongside as provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

from ..opc.ns import qn

# w:rFonts theme tokens -> (majorFont|minorFont, script element)
FONT_SLOTS = {
    "majorHAnsi": ("majorFont", "a:latin"),
    "majorAscii": ("majorFont", "a:latin"),
    "majorBidi": ("majorFont", "a:cs"),
    "majorEastAsia": ("majorFont", "a:ea"),
    "minorHAnsi": ("minorFont", "a:latin"),
    "minorAscii": ("minorFont", "a:latin"),
    "minorBidi": ("minorFont", "a:cs"),
    "minorEastAsia": ("minorFont", "a:ea"),
}

# w:themeColor tokens -> colour scheme element. The dk/lt naming in the theme
# does not match the text/background naming used in WordprocessingML.
COLOR_SLOTS = {
    "text1": "dk1", "background1": "lt1", "text2": "dk2", "background2": "lt2",
    "dark1": "dk1", "light1": "lt1", "dark2": "dk2", "light2": "lt2",
    "accent1": "accent1", "accent2": "accent2", "accent3": "accent3",
    "accent4": "accent4", "accent5": "accent5", "accent6": "accent6",
    "hyperlink": "hlink", "followedHyperlink": "folHlink",
}


@dataclass
class Theme:
    fonts: dict[str, str] = field(default_factory=dict)   # 'minorHAnsi' -> 'Calibri'
    colors: dict[str, str] = field(default_factory=dict)  # 'accent1' -> '4472C4'

    @classmethod
    def parse(cls, root: etree._Element | None) -> Theme:
        if root is None:
            return cls()
        fonts: dict[str, str] = {}
        for token, (scheme, script) in FONT_SLOTS.items():
            el = root.find(f".//{qn('a:' + scheme)}/{qn(script)}")
            if el is not None and (face := el.get("typeface")):
                fonts[token] = face
        colors: dict[str, str] = {}
        scheme_el = root.find(f".//{qn('a:clrScheme')}")
        if scheme_el is not None:
            for child in scheme_el:
                name = etree.QName(child).localname
                srgb = child.find(qn("a:srgbClr"))
                sys = child.find(qn("a:sysClr"))
                if srgb is not None and (v := srgb.get("val")):
                    colors[name] = v.upper()
                elif sys is not None and (v := sys.get("lastClr")):
                    colors[name] = v.upper()
        return cls(fonts, colors)

    def font(self, token: str | None) -> str | None:
        return self.fonts.get(token) if token else None

    def color(self, token: str | None) -> str | None:
        if not token or token == "none":
            return None
        return self.colors.get(COLOR_SLOTS.get(token, token))

    def resolve_font(self, explicit: str | None, token: str | None) -> str | None:
        """Theme reference wins over an explicit typeface, as Word resolves it."""
        return self.font(token) or explicit

    def resolve_color(
        self,
        explicit: str | None,
        token: str | None,
        tint: str | None = None,
        shade: str | None = None,
    ) -> str | None:
        """Resolve a colour, preferring Word's own cached value.

        ECMA 17.3.2.6 says w:val is ignored when themeColor is present, so the
        normative answer is to recompute from the theme. But Word writes the
        computed result into w:val, and recomputing requires themeTint/
        themeShade HSL math we have not implemented yet -- so preferring w:val
        is both correct in practice and strictly better than ignoring the tint.

        Without this, <w:color w:val="595959" w:themeColor="text1"
        w:themeTint="A6"/> -- the default Office heading grey -- resolves to
        pure black.
        """
        if explicit and explicit.lower() != "auto":
            return explicit.upper()
        if token:
            base = self.color(token)
            if base and (tint or shade):
                # TODO: HSL luminance math per [MS-OI29500] 2.1.71. Note that
                # themeTint wins when both are present, and that ECMA's own
                # published per-channel shade formula contradicts Word.
                return base
            return base
        return explicit
