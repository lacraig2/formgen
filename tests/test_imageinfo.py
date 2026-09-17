"""imageinfo reads the same size and DPI Pillow does.

This module replaced Pillow as a runtime dependency for one job: measuring an
embedded image. The only thing that makes that safe is parity -- if our numbers
drift from Pillow's, a generated document's image extent moves and the
byte-identical-across-engines guarantee is gone. So the test is not "does it
look plausible" but "is it Pillow, exactly", asserted against Pillow itself
over a matrix of formats, sizes and DPI settings.

Pillow is a dev/test dependency here (it writes the fixtures); it is not
imported anywhere under ``src/``.
"""

from __future__ import annotations

import pytest

from formgen.content.imageinfo import read_image

PILImage = pytest.importorskip("PIL.Image")


def _write(tmp_path, fmt, ext, size, save_kwargs, mode="RGB"):
    path = tmp_path / f"probe.{ext}"
    PILImage.new(mode, size).save(path, format=fmt, **save_kwargs)
    return path


def _pillow(path):
    with PILImage.open(path) as handle:
        return handle.size, handle.info.get("dpi")


# (format, extension, save-kwargs) -- the raster formats emit.py embeds and
# Pillow can decode on this platform. SVG/WMF/EMF are covered separately below.
RASTER = [
    ("PNG", "png", {}),
    ("PNG", "png", {"dpi": (300, 300)}),
    ("PNG", "png", {"dpi": (150, 200)}),
    ("JPEG", "jpg", {}),
    ("JPEG", "jpg", {"dpi": (72, 72)}),
    ("JPEG", "jpg", {"dpi": (300, 300)}),
    ("GIF", "gif", {}),
    ("BMP", "bmp", {}),
    ("BMP", "bmp", {"dpi": (200, 200)}),
    ("TIFF", "tif", {}),
    ("TIFF", "tif", {"dpi": (150, 150)}),
    ("TIFF", "tif", {"dpi": (600, 300)}),
]


@pytest.mark.parametrize("fmt,ext,kwargs", RASTER)
@pytest.mark.parametrize("size", [(120, 80), (1, 1), (3000, 1000), (37, 19)])
def test_matches_pillow(tmp_path, fmt, ext, kwargs, size):
    path = _write(tmp_path, fmt, ext, size, kwargs)
    want_size, want_dpi = _pillow(path)
    info = read_image(path)
    assert info is not None
    assert info.size == want_size
    # emit.py collapses a missing DPI to its default, so None and an absent key
    # are the same outcome. Everything present must match Pillow bit for bit.
    if want_dpi is None:
        assert info.dpi is None
    else:
        assert info.dpi == pytest.approx(tuple(float(d) for d in want_dpi), abs=0)


@pytest.mark.parametrize("mode", ["L", "P", "RGBA", "1"])
def test_png_size_across_pixel_modes(tmp_path, mode):
    path = _write(tmp_path, "PNG", "png", (64, 48), {}, mode=mode)
    assert read_image(path).size == (64, 48)


def test_a_png_without_phys_reports_no_dpi(tmp_path):
    path = _write(tmp_path, "PNG", "png", (10, 10), {})
    assert read_image(path).dpi is None


def test_unreadable_formats_return_none(tmp_path):
    # Vector metafiles Pillow does not decode here, plus outright garbage: all
    # must return None so emit.py falls back to its assumed 600x400.
    svg = tmp_path / "x.svg"
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" width="100"/>')
    junk = tmp_path / "x.png"
    junk.write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really a png")
    empty = tmp_path / "x.bmp"
    empty.write_bytes(b"")
    for path in (svg, junk, empty):
        assert read_image(path) is None


def test_truncated_jpeg_returns_none(tmp_path):
    path = tmp_path / "cut.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00")  # header only, no frame
    assert read_image(path) is None


def test_read_image_only_reads_the_header(tmp_path):
    # A megapixel image still measures from a handful of header bytes; this is
    # not a decode. The assertion is behavioural: size is right and it is fast.
    path = _write(tmp_path, "PNG", "png", (4000, 3000), {"dpi": (300, 300)})
    info = read_image(path)
    assert info.size == (4000, 3000)
    assert info.dpi == pytest.approx((300, 300), abs=0.01)
