"""PDF(待核件)渲染与预处理:PyMuPDF(§5.1)。

提供三类能力:
- get_page_metas      页面尺寸(供坐标归一化与 pdf.js 对齐)
- render_pages        按 DPI 渲染页面为 PNG(供真实 OCR 引擎)
- extract_text_blocks 提取文本层块(开发/Mock OCR 用,带 bbox)
"""
from __future__ import annotations

import logging
import math
import tempfile
from pathlib import Path

try:  # PyMuPDF 新版包名 pymupdf,旧版为 fitz
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore[no-redef]

from ..models import Block, PageMeta

logger = logging.getLogger(__name__)

# Pillow 对单张图片的默认安全阈值约为 8,948 万像素；但印章恢复还会创建
# RGB、int16 通道和掩码等多个副本。因此在渲染阶段采用更保守的预算，避免
# PNG 已生成后才由 Pillow 的 decompression-bomb 保护中断任务。
DEFAULT_MAX_RENDER_PIXELS = 30_000_000


def _page_render_scale(
    width_pt: float,
    height_pt: float,
    dpi: int,
    max_pixels: int | None,
) -> tuple[float, bool]:
    """返回单页安全渲染比例及是否因像素预算而降采样。"""
    requested_scale = dpi / 72.0
    if max_pixels is None:
        return requested_scale, False
    if max_pixels <= 0:
        raise ValueError("max_pixels 必须为正整数或 None")

    requested_pixels = width_pt * requested_scale * height_pt * requested_scale
    if requested_pixels <= max_pixels:
        return requested_scale, False

    # 先落到整数目标尺寸，再按较短边反推等比比例。PyMuPDF 最终按向上取整
    # 生成像素，故额外留出极小余量，确保实际 PNG 也不会越过预算。
    target_width = max(1, math.floor(math.sqrt(max_pixels * width_pt / height_pt)))
    target_height = max(1, math.floor(max_pixels / target_width))
    safe_scale = min(target_width / width_pt, target_height / height_pt)
    return safe_scale * (1 - 1e-9), True


def _page_pixel_size(
    width_pt: float,
    height_pt: float,
    scale: float,
) -> tuple[int, int]:
    """与 PyMuPDF matrix 渲染对应的页面像素尺寸。"""
    return max(1, round(width_pt * scale)), max(1, round(height_pt * scale))


def _render_page_pixmap(page, dpi: int, max_pixels: int | None):
    scale, reduced = _page_render_scale(
        page.rect.width, page.rect.height, dpi, max_pixels
    )
    if reduced:
        effective_dpi = scale * 72.0
        logger.warning(
            "pdf page render dpi reduced page=%s requested_dpi=%s effective_dpi=%.1f "
            "max_pixels=%s",
            page.number + 1,
            dpi,
            effective_dpi,
            max_pixels,
        )
    return page.get_pixmap(matrix=fitz.Matrix(scale, scale))


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


def get_page_metas(
    path: str | Path,
    dpi: int = 300,
    max_pixels: int | None = DEFAULT_MAX_RENDER_PIXELS,
) -> list[PageMeta]:
    """获取每页尺寸(pt 与渲染像素)。"""
    metas: list[PageMeta] = []
    with fitz.open(str(path)) as doc:
        for i, page in enumerate(doc):
            w_pt, h_pt = page.rect.width, page.rect.height
            scale, _ = _page_render_scale(w_pt, h_pt, dpi, max_pixels)
            width_px, height_px = _page_pixel_size(w_pt, h_pt, scale)
            metas.append(
                PageMeta(
                    page_index=i,
                    width_px=width_px,
                    height_px=height_px,
                    pdf_width_pt=w_pt,
                    pdf_height_pt=h_pt,
                )
            )
    return metas


def render_pages(
    path: str | Path,
    dpi: int = 300,
    max_pixels: int | None = DEFAULT_MAX_RENDER_PIXELS,
) -> list[bytes]:
    """渲染每页为 PNG 字节流(交给真实 OCR 引擎)。

    一次性打开文档遍历所有页,适合需要全部页面的场景(如 OCR)。
    若只需某一页,用 render_page 避免全量渲染。
    """
    images: list[bytes] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            pix = _render_page_pixmap(page, dpi, max_pixels)
            images.append(pix.tobytes("png"))
    return images


def render_page(
    path: str | Path,
    page_index: int,
    dpi: int = 300,
    max_pixels: int | None = DEFAULT_MAX_RENDER_PIXELS,
) -> bytes:
    """渲染指定页为 PNG 字节流(惰性按页渲染)。

    供 LLM 兜底等「只需个别页面」的场景调用,避免对整份 PDF 做全量渲染。
    """
    with fitz.open(str(path)) as doc:
        page = doc.load_page(page_index)
        pix = _render_page_pixmap(page, dpi, max_pixels)
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
