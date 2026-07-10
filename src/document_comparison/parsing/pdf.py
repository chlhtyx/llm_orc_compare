"""PDF(待核件)渲染与预处理:PyMuPDF(§5.1)。

提供三类能力:
- get_page_metas      页面尺寸(供坐标归一化与 pdf.js 对齐)
- render_pages        按 DPI 渲染页面为 PNG(供真实 OCR 引擎)
- extract_text_blocks 提取文本层块(开发/Mock OCR 用,带 bbox)
"""
from __future__ import annotations

from pathlib import Path

try:  # PyMuPDF 新版包名 pymupdf,旧版为 fitz
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from ..models import Block, PageMeta


def get_page_metas(path: str | Path, dpi: int = 300) -> list[PageMeta]:
    """获取每页尺寸(pt 与渲染像素)。"""
    scale = dpi / 72.0
    metas: list[PageMeta] = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc):
            w_pt, h_pt = page.rect.width, page.rect.height
            metas.append(
                PageMeta(
                    page_index=i,
                    width_px=round(w_pt * scale),
                    height_px=round(h_pt * scale),
                    pdf_width_pt=w_pt,
                    pdf_height_pt=h_pt,
                )
            )
    return metas


def render_pages(path: str | Path, dpi: int = 300) -> list[bytes]:
    """渲染每页为 PNG 字节流(交给真实 OCR 引擎)。"""
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    images: list[bytes] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            images.append(pix.tobytes("png"))
    return images


def extract_text_blocks(path: str | Path) -> list[list[Block]]:
    """提取 PDF 文本层块(开发/Mock OCR 用)。

    bbox 为 PDF 点坐标(pt),与 pdf.js viewport 同坐标系。
    对扫描件(无文本层)返回空,此时须切换真实 OCR 引擎。
    """
    pages: list[list[Block]] = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc):
            blocks: list[Block] = []
            for idx, raw in enumerate(page.get_text("blocks")):
                x0, y0, x1, y1, text = raw[0], raw[1], raw[2], raw[3], raw[4]
                text = (text or "").strip()
                if not text:
                    continue
                blocks.append(
                    Block(
                        block_id=f"p{i}-b{idx}",
                        page_index=i,
                        label="text",
                        bbox=[x0, y0, x1, y1],
                        content=text,
                    )
                )
            pages.append(blocks)
    return pages
