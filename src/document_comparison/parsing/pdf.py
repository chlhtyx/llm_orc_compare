"""PDF(待核件)渲染与预处理:PyMuPDF(§5.1)。

提供三类能力:
- get_page_metas      页面尺寸(供坐标归一化与 pdf.js 对齐)
- render_pages        按 DPI 渲染页面为 PNG(供真实 OCR 引擎)
- extract_text_blocks 提取文本层块(开发/Mock OCR 用,带 bbox)
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

try:  # PyMuPDF 新版包名 pymupdf,旧版为 fitz
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from ..models import Block, PageMeta

logger = logging.getLogger(__name__)


def count_pages(path: str | Path) -> int:
    """返回 PDF 页数(打开即关,不做渲染)。

    供提交前的页数上限预检使用:比 get_page_metas / render_pages 轻量,
    不读取页面内容,只用 len(doc) 取页数。
    """
    with fitz.open(str(path)) as doc:
        return len(doc)


def count_pages_from_bytes(data: bytes) -> int:
    """与 count_pages 相同,但接收内存中的 PDF 字节(供上传预检使用)。"""
    with fitz.open(stream=data, filetype="pdf") as doc:
        return len(doc)


def slice_pdf(
    src: str | Path,
    n_pages: int,
    output_path: str | Path | None = None,
) -> Path:
    """生成只含前 n_pages 页的 PDF,返回其路径。

    用于「回收件页数截取」:回收 PDF 页数超过原始合同时,物理截断到原始页数
    再走流水线,使 OCR / build_report / burn_pdf / 高亮图渲染等全部下游在
    页数维度上自动一致(下游按 `len(doc)` 逐页遍历并索引 `page_meta`)。

    `n_pages <= 0` 或 `n_pages >= 现有页数` 时直接返回原路径(不复制),避免
    无意义或越界的截断(PyMuPDF 的 to_page=-1 会复制全部页,语义反直觉)。
    """
    src = Path(src)
    n_pages = int(n_pages)
    if n_pages <= 0:
        return src
    with fitz.open(str(src)) as doc:
        total = len(doc)
        if n_pages >= total:
            return src
        if output_path is None:
            out_path = Path(tempfile.mkstemp(prefix="dc-slice-", suffix=".pdf")[1])
        else:
            out_path = Path(output_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
        subset = fitz.open()
        try:
            subset.insert_pdf(doc, from_page=0, to_page=n_pages - 1)
            subset.save(str(out_path))
        finally:
            subset.close()
    logger.info("pdf sliced src=%s total=%s -> %s pages", src.name, total, n_pages)
    return out_path


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
    """渲染每页为 PNG 字节流(交给真实 OCR 引擎)。

    一次性打开文档遍历所有页,适合需要全部页面的场景(如 OCR)。
    若只需某一页,用 render_page 避免全量渲染。
    """
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    images: list[bytes] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            images.append(pix.tobytes("png"))
    return images


def render_page(path: str | Path, page_index: int, dpi: int = 300) -> bytes:
    """渲染指定页为 PNG 字节流(惰性按页渲染)。

    供 LLM 兜底等「只需个别页面」的场景调用,避免对整份 PDF 做全量渲染。
    """
    scale = dpi / 72.0
    matrix = fitz.Matrix(scale, scale)
    with fitz.open(str(path)) as doc:
        page = doc.load_page(page_index)
        pix = page.get_pixmap(matrix=matrix)
        return pix.tobytes("png")


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
    # 全部页都无文本块 → 多半是扫描件,提示须走真实 OCR
    if not any(pages):
        logger.info("pdf text layer empty (scanned?), need real OCR path=%s", path)
    return pages
