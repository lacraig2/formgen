"""Read an image's pixel size and DPI without Pillow.

Pillow was a runtime dependency for exactly one thing: telling ``emit.py`` how
large an embedded image is, so it can be scaled to fit the text column. That is
a few bytes of header per format, so we read them ourselves and keep the Pillow
wheel out of the browser bundle (it is the single largest thing Pyodide had to
download for a tool that otherwise never decodes a pixel).

The numbers here are Pillow's, not merely close to them. ``size`` and ``dpi``
are computed with the exact constants and integer arithmetic Pillow uses --
PNG's ``px * 0.0254``, BMP's ``ppm / 39.3701``, TIFF's resolution rationals --
because ``emit.py`` turns them into EMU and a different float would move a
``w:ext`` by a hair and break the byte-identical-in-a-browser guarantee.
``tests/test_imageinfo.py`` asserts that parity against Pillow across a matrix
of formats, sizes and DPI settings; it is the actual contract for this module.

A format we cannot read returns ``None`` -- the same outcome Pillow's raising
produced, so the caller's "assumed 600x400" fallback is unchanged. SVG, WMF and
EMF are vector metafiles Pillow does not decode on this platform; they fall
into that branch exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from struct import error as StructError
from struct import unpack


@dataclass(frozen=True)
class ImageInfo:
    """What ``emit.py`` asks Pillow for: pixel size and, if the file records
    one, physical resolution. ``dpi`` is ``None`` when the format carries no
    resolution, which is exactly when Pillow leaves ``info["dpi"]`` unset."""

    size: tuple[int, int]
    dpi: tuple[float, float] | None = None


def read_image(path: Path) -> ImageInfo | None:
    """Native pixel size and DPI, or ``None`` if the header is unreadable.

    Reads only the header, never the pixels. ``None`` means "let the caller
    fall back", covering both unsupported formats and truncated files -- the
    same cases in which the Pillow version raised.
    """
    try:
        with open(path, "rb") as handle:
            return read_image_bytes(handle.read())
    except OSError:
        return None


def read_image_bytes(data: bytes) -> ImageInfo | None:
    """Like :func:`read_image`, for an image already held in memory.

    `fill` reaches this path: an uploaded image arrives as bytes, and it needs
    the native size to fit the picture into the slot the template author drew.
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return _png(data)
        if data[:2] == b"\xff\xd8":
            return _jpeg(data)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return _gif(data)
        if data[:2] == b"BM":
            return _bmp(data)
        if data[:4] in (b"II*\x00", b"MM\x00*"):
            return _tiff(data)
    except (ValueError, IndexError, StructError):
        return None
    return None


# Magic bytes -> (extension, Word-embeddable content type). The extension is
# what emit.py's MEDIA_TYPES keys on, so the two agree on what Word will take.
def sniff(data: bytes) -> tuple[str, str] | None:
    """The file extension and media type for an image, from its first bytes.

    Returned so a caller with only the bytes (an upload, say) can name the
    media part and register its content type without trusting a filename.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png", "image/png"
    if data[:2] == b"\xff\xd8":
        return ".jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    if data[:2] == b"BM":
        return ".bmp", "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tiff", "image/tiff"
    return None


# -- PNG ---------------------------------------------------------------------

def _png(data: bytes) -> ImageInfo | None:
    """IHDR gives the size; a pHYs chunk in meters gives the DPI.

    Pillow only reports a DPI when pHYs says its unit is the meter, so a PNG
    without pHYs (the common case) leaves DPI unset and the caller assumes 96.
    """
    if data[12:16] != b"IHDR":
        return None
    width, height = unpack(">II", data[16:24])
    dpi = None
    pos = 8
    limit = len(data)
    while pos + 8 <= limit:
        length = unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if kind == b"pHYs" and length >= 9:
            px, py = unpack(">II", body[0:8])
            if body[8] == 1:  # unit is the meter; anything else is aspect-only
                dpi = (px * 0.0254, py * 0.0254)
            break
        if kind == b"IDAT":  # pHYs is always ahead of the pixels; stop here
            break
        pos += 12 + length  # length + type + data + CRC
    return ImageInfo((width, height), dpi)


# -- JPEG --------------------------------------------------------------------

def _jpeg(data: bytes) -> ImageInfo | None:
    """Walk the marker segments for the frame header, JFIF density and EXIF.

    Size comes from the SOF frame header. DPI follows Pillow's precedence: a
    JFIF APP0 density wins, and only if absent is EXIF resolution consulted.
    """
    size: tuple[int, int] | None = None
    dpi: tuple[float, float] | None = None
    exif: bytes | None = None
    pos = 2
    limit = len(data)
    while pos + 4 <= limit:
        if data[pos] != 0xFF:
            return None
        # A marker may be preceded by any number of 0xFF fill bytes; skip them
        # so a padded stream still reaches its frame header.
        while pos + 1 < limit and data[pos + 1] == 0xFF:
            pos += 1
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = unpack(">H", data[pos + 2 : pos + 4])[0]
        segment = data[pos + 4 : pos + 2 + length]
        if marker == 0xE0 and segment[:5] == b"JFIF\x00" and dpi is None:
            unit = segment[7]
            xd, yd = unpack(">HH", segment[8:12])
            if unit == 1:            # dots per inch
                dpi = (float(xd), float(yd))
            elif unit == 2:          # dots per centimeter
                dpi = (xd * 2.54, yd * 2.54)
        elif marker == 0xE1 and segment[:6] == b"Exif\x00\x00" and exif is None:
            exif = segment[6:]
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # a Start-Of-Frame: precision, then height, then width
            height, width = unpack(">HH", segment[1:5])
            size = (width, height)
            break
        pos += 2 + length
    if size is None:
        return None
    if dpi is None and exif is not None:
        dpi = _exif_dpi(exif)
    return ImageInfo(size, dpi)


def _exif_dpi(exif: bytes) -> tuple[float, float] | None:
    """Pillow's EXIF fallback: X-resolution for *both* axes, cm converted.

    It deliberately uses XResolution alone -- see ``_read_dpi_from_exif`` in
    Pillow -- so we do too, or a rare EXIF-only JPEG would differ by a pixel.
    """
    tags = _ifd_tags(exif)
    if tags is None or 0x011A not in tags:
        return None
    xres = _as_number(tags[0x011A])
    if xres is None:
        return None
    unit = _as_number(tags.get(0x0128))
    if unit == 3:  # centimeter
        xres *= 2.54
    return (xres, xres)


# -- GIF ---------------------------------------------------------------------

def _gif(data: bytes) -> ImageInfo:
    """The logical screen descriptor, right after the six-byte signature. GIF
    records no resolution, so DPI stays unset just as Pillow leaves it."""
    width, height = unpack("<HH", data[6:10])
    return ImageInfo((width, height), None)


# -- BMP ---------------------------------------------------------------------

def _bmp(data: bytes) -> ImageInfo | None:
    """The DIB header. The ancient 12-byte core header carries no resolution;
    every later header stores pixels-per-meter, which Pillow divides by
    39.3701 -- not by 0.0254 -- so we match that constant exactly."""
    if len(data) < 26:
        return None
    header_size = unpack("<I", data[14:18])[0]
    body = data[18:]  # the DIB header past its own 4-byte size field
    if header_size == 12:
        width, height = unpack("<HH", body[0:4])
        return ImageInfo((width, height), None)
    if header_size in (40, 52, 56, 64, 108, 124):
        width = unpack("<I", body[0:4])[0]
        raw_height = unpack("<I", body[4:8])[0]
        # data[7] of the header (body[3]) == 0xFF marks a top-down bitmap,
        # whose height Pillow reads as 2**32 - value.
        height = (2**32 - raw_height) if body[3] == 0xFF else raw_height
        xppm, yppm = unpack("<ii", body[20:28])
        return ImageInfo((width, height), (xppm / 39.3701, yppm / 39.3701))
    return None


# -- TIFF --------------------------------------------------------------------

_TIFF_WIDTH = 256
_TIFF_HEIGHT = 257
_TIFF_XRES = 282
_TIFF_YRES = 283
_TIFF_RESUNIT = 296


def _tiff(data: bytes) -> ImageInfo | None:
    """The first IFD. Resolution is a rational; the unit tag decides how to
    read it, defaulting to inches when absent -- Pillow's current default,
    which used to be 1 and is now 2."""
    tags = _ifd_tags(data)
    if tags is None:
        return None
    width = _as_number(tags.get(_TIFF_WIDTH))
    height = _as_number(tags.get(_TIFF_HEIGHT))
    if width is None or height is None:
        return None
    dpi = None
    # Pillow reads resolution with a default of 1, so a TIFF that omits the
    # tags still reports (1, 1) rather than nothing -- match that.
    xres = _as_number(tags.get(_TIFF_XRES, 1))
    yres = _as_number(tags.get(_TIFF_YRES, 1))
    if xres and yres:
        unit = _as_number(tags.get(_TIFF_RESUNIT))
        if unit == 3:  # centimeter
            dpi = (xres * 2.54, yres * 2.54)
        elif unit in (2, None):  # inch, or the modern default
            dpi = (xres, yres)
        # any other unit is "no absolute measure": Pillow leaves dpi unset
    return ImageInfo((int(width), int(height)), dpi)


# -- a small TIFF IFD reader, shared by TIFF files and JPEG EXIF -------------

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8}


def _ifd_tags(data: bytes) -> dict[int, object] | None:
    """Parse the first IFD of a TIFF byte stream into ``tag -> value(s)``.

    Values are ints for SHORT/LONG and ``(num, den)`` for RATIONAL, which is
    all this module needs; the caller reduces them with ``_as_number``.
    """
    if len(data) < 8:
        return None
    order = data[:2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        return None
    (offset,) = unpack(endian + "I", data[4:8])
    if offset + 2 > len(data):
        return None
    (count,) = unpack(endian + "H", data[offset : offset + 2])
    tags: dict[int, object] = {}
    entry = offset + 2
    for _ in range(count):
        if entry + 12 > len(data):
            break
        tag, kind, n = unpack(endian + "HHI", data[entry : entry + 8])
        size = _TYPE_SIZES.get(kind, 0) * n
        if size == 0:
            entry += 12
            continue
        if size <= 4:
            payload = data[entry + 8 : entry + 12]  # value packed inline
        else:
            start = _read_u32(data, entry + 8, endian)  # value stored at offset
            payload = data[start : start + size]
        tags[tag] = _decode(kind, n, payload, endian)
        entry += 12
    return tags


def _read_u32(data: bytes, at: int, endian: str) -> int:
    return unpack(endian + "I", data[at : at + 4])[0]


def _decode(kind: int, n: int, payload: bytes, endian: str):
    if kind in (3, 8):  # SHORT
        return unpack(endian + "H", payload[:2])[0]
    if kind in (4, 9):  # LONG
        return unpack(endian + "I", payload[:4])[0]
    if kind in (5, 10):  # RATIONAL: numerator, denominator
        num, den = unpack(endian + "II", payload[:8])
        return (num, den)
    if kind in (1, 6, 7):  # BYTE-ish
        return payload[0] if payload else 0
    return None


def _as_number(value) -> float | int | None:
    """Reduce an IFD value to the single number the resolution maths wants.

    A rational becomes ``num / den`` as a float -- the same value Pillow's
    ``IFDRational`` yields when ``emit.py`` calls ``float()`` on it -- because
    Python's true division and ``float(Fraction(num, den))`` round identically.
    """
    if value is None:
        return None
    if isinstance(value, tuple):
        num, den = value
        if den == 0:
            return None
        return num / den
    return value
