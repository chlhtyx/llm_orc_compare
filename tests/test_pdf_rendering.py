from io import BytesIO

import pytest

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

from PIL import Image

from document_comparison.parsing.pdf import get_page_metas, render_page


def _single_page_pdf(path, *, width: float, height: float) -> None:
    with fitz.open() as document:
        document.new_page(width=width, height=height)
        document.save(path)


def test_oversized_pdf_page_is_rendered_under_pixel_budget(tmp_path):
    """超大 PDF 页面降 DPI 后，元数据和实际 PNG 尺寸必须保持一致。"""
    pdf_path = tmp_path / "oversized.pdf"
    _single_page_pdf(pdf_path, width=1_000, height=2_000)

    max_pixels = 1_000_000
    meta = get_page_metas(pdf_path, dpi=300, max_pixels=max_pixels)[0]
    png = render_page(pdf_path, 0, dpi=300, max_pixels=max_pixels)

    with Image.open(BytesIO(png)) as image:
        assert image.width * image.height <= max_pixels
        assert image.size == (meta.width_px, meta.height_px)


def test_normal_pdf_page_keeps_requested_dpi(tmp_path):
    pdf_path = tmp_path / "normal.pdf"
    _single_page_pdf(pdf_path, width=72, height=144)

    meta = get_page_metas(pdf_path, dpi=200, max_pixels=1_000_000)[0]

    assert (meta.width_px, meta.height_px) == (200, 400)
